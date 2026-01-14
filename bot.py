import os
import re
import json
import time
import html
import hashlib
import logging
from typing import Dict, Any, Tuple, List, Optional

import requests
from bs4 import BeautifulSoup

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ----------------------------
# SETTINGS
# ----------------------------

BOT_NAME = "AutoSuchBot"
DATA_FILE = "search_data.json"

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "180"))  # alle 3 Minuten

DEFAULT_RADIUS_KM = 300
DEFAULT_ONLY_WITH_PHOTOS = True

BLACKLIST_WORDS = [
    "unfall",
    "bastler",
    "motorschaden",
    "defekt",
    "export",
    "teileträger",
    "ohne tüv",
    "ohne tuv",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AutoSuchBot/1.0)"
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ----------------------------
# STORAGE
# ----------------------------

def load_data() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        return {"searches": [], "seen_ids": []}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"searches": [], "seen_ids": []}


def save_data(data: Dict[str, Any]):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def make_seen_id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ----------------------------
# HELPERS
# ----------------------------

def parse_int(x: str) -> Optional[int]:
    try:
        return int(re.sub(r"[^\d]", "", x))
    except Exception:
        return None


def contains_blacklist(text: str) -> bool:
    t = text.lower()
    for w in BLACKLIST_WORDS:
        if w in t:
            return True
    return False


def parse_set_args(text: str) -> Tuple[str, Dict[str, Any]]:
    """
    /set Audi a6 10000 67157 300 bj2010
    query....  price   PLZ   radius  bjYYYY
    """
    raw = text.strip()
    raw = re.sub(r"^/set\s*", "", raw).strip()

    parts = re.findall(r'"[^"]+"|\S+', raw)
    parts = [p.strip().strip('"') for p in parts]

    data = {
        "price_to": None,
        "radius_km": DEFAULT_RADIUS_KM,
        "location": "DE",
        "year_from": None,
        "only_photos": DEFAULT_ONLY_WITH_PHOTOS,
    }

    # BJ erkennen: bj2010
    new_parts = []
    for p in parts:
        m = re.match(r"^bj(\d{4})$", p.lower())
        if m:
            data["year_from"] = int(m.group(1))
            continue
        new_parts.append(p)
    parts = new_parts

    query_tokens = []
    numbers = []

    for p in parts:
        if re.fullmatch(r"\d+", p):
            numbers.append(p)
        else:
            query_tokens.append(p)

    price = None
    plz = None
    radius = None

    for n in numbers:
        if len(n) == 5 and plz is None:
            plz = n
            continue
        if len(n) >= 3 and price is None:
            price = n
            continue

    for n in reversed(numbers):
        if len(n) <= 4 and (price is None or n != price):
            radius = n
            break

    if price:
        data["price_to"] = int(price)

    if plz:
        data["location"] = plz

    if radius:
        data["radius_km"] = int(radius)

    query = " ".join(query_tokens).strip()
    if not query:
        query = "autos"

    return query, data


# ----------------------------
# SEARCH BUILDERS
# ----------------------------

def build_mobile_url(query: str, price_to: Optional[int], plz: str, radius: int, year_from: Optional[int]) -> str:
    base = "https://suchen.mobile.de/fahrzeuge/search.html"
    params = {
        "isSearchRequest": "true",
        "vc": "Car",
        "dam": "0",
        "sb": "rel",
        "cn": "DE",
        "maxPrice": str(price_to) if price_to else "",
        "rad": str(radius),
        "zipcode": plz,
        "lang": "de",
        "ref": "quickSearch",
        "ft": "PETROL_DIESEL",  # optional, kann raus wenn du willst
    }

    # query -> als "makeModelVariant1.modelDescription" ist nicht perfekt
    # besser: als "q"
    params["q"] = query

    if year_from:
        params["minFirstRegistrationDate"] = str(year_from)

    # URL zusammenbauen
    q = "&".join([f"{k}={requests.utils.quote(str(v))}" for k, v in params.items() if v != ""])
    return f"{base}?{q}"


def build_kleinanzeigen_url(query: str, price_to: Optional[int]) -> str:
    # Kleinanzeigen ist leider nicht so schön parametrisierbar wie mobile
    base = "https://www.kleinanzeigen.de/s-autos/k0"
    q = requests.utils.quote(query)

    # Preisfilter
    if price_to:
        return f"{base}?keywords={q}&priceTo={price_to}"
    return f"{base}?keywords={q}"


# ----------------------------
# FETCH + FILTER RESULTS
# ----------------------------

def fetch_mobile_results(url: str) -> List[Dict[str, Any]]:
    """
    Sehr simpel (weil mobile viele dynamische Inhalte hat).
    Wir bauen erstmal NUR Link-Liste aus HTML - funktioniert oft trotzdem.
    """
    out = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return out

        soup = BeautifulSoup(r.text, "lxml")

        # Links auf Angebote
        links = soup.select("a[href*='/fahrzeuge/details.html?id=']")
        seen = set()

        for a in links:
            href = a.get("href", "").strip()
            if not href:
                continue

            if href.startswith("/"):
                href = "https://suchen.mobile.de" + href

            if href in seen:
                continue
            seen.add(href)

            title = a.get_text(" ", strip=True)
            if not title:
                title = "mobile.de Anzeige"

            out.append({
                "source": "mobile.de",
                "title": title,
                "url": href,
                "text": title.lower(),
                "has_photo": True,  # mobile hat fast immer Bilder
            })

    except Exception as e:
        logger.warning(f"mobile fetch error: {e}")

    return out


def fetch_kleinanzeigen_results(url: str) -> List[Dict[str, Any]]:
    out = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return out

        soup = BeautifulSoup(r.text, "lxml")

        items = soup.select("article.aditem")
        for it in items:
            a = it.select_one("a[href*='/s-anzeige/']")
            if not a:
                continue

            href = a.get("href", "").strip()
            if not href:
                continue

            if href.startswith("/"):
                href = "https://www.kleinanzeigen.de" + href

            title_el = it.select_one("h2 a")
            title = title_el.get_text(" ", strip=True) if title_el else "Kleinanzeigen Anzeige"

            desc_el = it.select_one(".aditem-main--middle")
            desc = desc_el.get_text(" ", strip=True) if desc_el else ""

            img_el = it.select_one("img")
            has_photo = img_el is not None and img_el.get("src") is not None

            out.append({
                "source": "Kleinanzeigen",
                "title": title,
                "url": href,
                "text": (title + " " + desc).lower(),
                "has_photo": has_photo,
            })

    except Exception as e:
        logger.warning(f"kleinanzeigen fetch error: {e}")

    return out


def filter_results(results: List[Dict[str, Any]], only_photos: bool, year_from: Optional[int]) -> List[Dict[str, Any]]:
    filtered = []
    for r in results:
        # Blacklist
        if contains_blacklist(r.get("text", "")):
            continue

        # Nur mit Bildern
        if only_photos and not r.get("has_photo", False):
            continue

        # Year filtering ist schwer ohne Details -> wir filtern nur indirekt über mobile parameter
        # Kleinanzeigen: kein echtes BJ -> lassen wir so.
        filtered.append(r)

    return filtered


# ----------------------------
# TELEGRAM COMMANDS
# ----------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        f"✅ {BOT_NAME} online.\n\n"
        "Benutzung:\n"
        "/set audi a6 6000 67157 300 bj2016\n\n"
        "Andere:\n"
        "/list\n"
        "/del 1\n"
        "/stop\n"
    )
    await update.message.reply_text(msg)


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    query, data = parse_set_args(text)

    store = load_data()

    search_entry = {
        "query": query,
        "price_to": data["price_to"],
        "radius_km": data["radius_km"],
        "location": data["location"],  # PLZ
        "year_from": data["year_from"],
        "only_photos": data["only_photos"],
        "chat_id": update.effective_chat.id,
    }

    store["searches"].append(search_entry)
    save_data(store)

    plz = search_entry["location"]
    mobile_url = build_mobile_url(query, search_entry["price_to"], plz, search_entry["radius_km"], search_entry["year_from"])
    klein_url = build_kleinanzeigen_url(query, search_entry["price_to"])

    reply = (
        "✅ Suche gespeichert!\n\n"
        f"🚗 {query}\n"
        f"💶 bis {search_entry['price_to']}€\n"
        f"📍 {plz} ({search_entry['radius_km']} km)\n"
    )

    if search_entry["year_from"]:
        reply += f"📅 ab Baujahr {search_entry['year_from']}\n"

    reply += "\n🔎 Kleinanzeigen:\n" + klein_url + "\n\n"
    reply += "🔎 mobile.de:\n" + mobile_url + "\n\n"
    reply += "⏱️ Alerts laufen jetzt automatisch."

    await update.message.reply_text(reply)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store = load_data()
    chat_id = update.effective_chat.id

    items = [s for s in store["searches"] if s["chat_id"] == chat_id]

    if not items:
        await update.message.reply_text("Keine gespeicherten Suchen.")
        return

    lines = ["📋 Deine gespeicherten Suchen:\n"]
    for idx, s in enumerate(items, start=1):
        extra = ""
        if s.get("year_from"):
            extra += f" bj≥{s['year_from']}"
        lines.append(
            f"{idx}) {s['query']} | max {s['price_to']}€ | {s['location']} | {s['radius_km']}km{extra}"
        )

    await update.message.reply_text("\n".join(lines))


async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store = load_data()
    chat_id = update.effective_chat.id

    parts = update.message.text.strip().split()
    if len(parts) < 2:
        await update.message.reply_text("Benutzung: /del 1")
        return

    idx = parse_int(parts[1])
    if not idx:
        await update.message.reply_text("Ungültige Nummer.")
        return

    items = [s for s in store["searches"] if s["chat_id"] == chat_id]

    if idx < 1 or idx > len(items):
        await update.message.reply_text("Diese Suche gibt es nicht.")
        return

    to_delete = items[idx - 1]
    store["searches"].remove(to_delete)
    save_data(store)

    await update.message.reply_text(f"🗑️ Suche gelöscht: {to_delete['query']}")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store = load_data()
    chat_id = update.effective_chat.id

    before = len(store["searches"])
    store["searches"] = [s for s in store["searches"] if s["chat_id"] != chat_id]
    after = len(store["searches"])

    save_data(store)

    await update.message.reply_text(f"⛔️ Alle Suchen gelöscht. ({before-after} entfernt)")


# ----------------------------
# ALERT LOOP
# ----------------------------

async def alert_job(context: ContextTypes.DEFAULT_TYPE):
    store = load_data()

    if not store.get("searches"):
        return

    seen_ids = set(store.get("seen_ids", []))

    for s in store["searches"]:
        query = s["query"]
        price_to = s.get("price_to")
        radius = s.get("radius_km", DEFAULT_RADIUS_KM)
        plz = s.get("location", "DE")
        year_from = s.get("year_from")
        only_photos = s.get("only_photos", True)

        mobile_url = build_mobile_url(query, price_to, plz, radius, year_from)
        klein_url = build_kleinanzeigen_url(query, price_to)

        mobile_results = fetch_mobile_results(mobile_url)
        klein_results = fetch_kleinanzeigen_results(klein_url)

        all_results = mobile_results + klein_results
        all_results = filter_results(all_results, only_photos=only_photos, year_from=year_from)

        new_hits = []
        for r in all_results:
            rid = make_seen_id(r["url"])
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            new_hits.append(r)

        # Limit Spam
        new_hits = new_hits[:5]

        for hit in new_hits:
            msg = (
                f"🚨 Neuer Treffer!\n"
                f"🌍 {hit['source']}\n"
                f"🚗 {hit['title']}\n"
                f"🔗 {hit['url']}"
            )
            try:
                await context.bot.send_message(chat_id=s["chat_id"], text=msg)
            except Exception as e:
                logger.warning(f"send_message error: {e}")

    store["seen_ids"] = list(seen_ids)[-5000:]
    save_data(store)


# ----------------------------
# MAIN
# ----------------------------

def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN fehlt als Environment Variable")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("stop", cmd_stop))

    # Job Queue
    app.job_queue.run_repeating(alert_job, interval=CHECK_INTERVAL_SECONDS, first=10)

    logger.info("Bot gestartet...")
    app.run_polling()


if __name__ == "__main__":
    main()
