import os
import re
import json
import time
import hashlib
import logging
from typing import Dict, Any, List, Optional, Tuple

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

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))  # ✅ jede Minute

DEFAULT_RADIUS_KM = 300
DEFAULT_ONLY_WITH_PHOTOS = True

MAX_PER_RUN = 10  # ✅ Limit pro Minute = 10 Treffer

BLACKLIST_WORDS = [
    "unfall",
    "bastler",
    "motorschaden",
    "defekt",
    "export",
    "teileträger",
    "teiletraeger",
    "ohne tüv",
    "ohne tuv",
]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AutoSuchBot/1.0)"}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ----------------------------
# STORAGE
# ----------------------------

def load_data() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        return {"searches": []}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"searches": []}


def save_data(data: Dict[str, Any]):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def parse_int(x: str) -> Optional[int]:
    try:
        return int(re.sub(r"[^\d]", "", x))
    except Exception:
        return None


def contains_blacklist(text: str) -> bool:
    t = (text or "").lower()
    return any(w in t for w in BLACKLIST_WORDS)


def make_id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ----------------------------
# /set Parser (NEUE Reihenfolge!)
# Reihenfolge: Auto/Model/PLZ/Preis/BJ
# Beispiel: /set Audi a6 67157 10000 bj2010
# ----------------------------

def parse_set_args(text: str) -> Dict[str, Any]:
    raw = text.strip()
    raw = re.sub(r"^/set\s*", "", raw).strip()

    if not raw:
        return {}

    parts = raw.split()

    # BJ erkennen (bj2010)
    year_from = None
    bj_token = None
    for p in parts:
        m = re.match(r"^bj(\d{4})$", p.lower())
        if m:
            year_from = int(m.group(1))
            bj_token = p
            break

    if bj_token:
        parts.remove(bj_token)

    # Mindest-Format: auto model plz price
    if len(parts) < 4:
        return {}

    # PLZ ist 5-stellig
    plz = parts[-2]  # Auto Model ... PLZ Preis
    price = parts[-1]

    if not re.fullmatch(r"\d{5}", plz):
        return {}

    if not re.fullmatch(r"\d{3,8}", price):
        return {}

    query_tokens = parts[:-2]
    query = " ".join(query_tokens).strip()

    return {
        "query": query.lower(),
        "plz": plz,
        "price_to": int(price),
        "radius_km": DEFAULT_RADIUS_KM,
        "year_from": year_from,
        "only_photos": DEFAULT_ONLY_WITH_PHOTOS,
    }


# ----------------------------
# SEARCH URLS
# ----------------------------

def build_mobile_url(query: str, price_to: int, plz: str, radius: int, year_from: Optional[int]) -> str:
    base = "https://suchen.mobile.de/fahrzeuge/search.html"
    params = {
        "isSearchRequest": "true",
        "vc": "Car",
        "dam": "0",
        "sb": "rel",
        "cn": "DE",
        "zipcode": plz,
        "rad": str(radius),
        "maxPrice": str(price_to),
        "q": query,
    }

    if year_from:
        params["minFirstRegistrationDate"] = str(year_from)

    q = "&".join([f"{k}={requests.utils.quote(str(v))}" for k, v in params.items()])
    return f"{base}?{q}"


def build_kleinanzeigen_url(query: str, price_to: int) -> str:
    base = "https://www.kleinanzeigen.de/s-autos/k0c216"
    q = requests.utils.quote(query)
    return f"{base}?keywords={q}&priceTo={price_to}"


# ----------------------------
# FETCH RESULTS (best effort)
# ----------------------------

def fetch_mobile_results(url: str) -> List[Dict[str, Any]]:
    out = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return out

        soup = BeautifulSoup(r.text, "lxml")
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

            title = a.get_text(" ", strip=True) or "mobile.de Anzeige"

            out.append({
                "source": "mobile.de",
                "title": title,
                "url": href,
                "text": title.lower(),
                "has_photo": True,
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

        # Artikel auf Suchseite
        for a in soup.select("a[href*='/s-anzeige/']"):
            href = a.get("href", "").strip()
            if not href:
                continue

            if href.startswith("/"):
                href = "https://www.kleinanzeigen.de" + href

            title = a.get_text(" ", strip=True) or "Kleinanzeigen Anzeige"
            title_low = title.lower()

            out.append({
                "source": "Kleinanzeigen",
                "title": title,
                "url": href,
                "text": title_low,
                "has_photo": True,  # Kleinanzeigen hat fast immer Bild-Kacheln
            })

    except Exception as e:
        logger.warning(f"kleinanzeigen fetch error: {e}")

    # Duplikate raus
    uniq = {}
    for x in out:
        uniq[x["url"]] = x
    return list(uniq.values())


def apply_filters(results: List[Dict[str, Any]], only_photos: bool) -> List[Dict[str, Any]]:
    filtered = []
    for r in results:
        if contains_blacklist(r.get("text", "")):
            continue
        if only_photos and not r.get("has_photo", False):
            continue
        filtered.append(r)
    return filtered


# ----------------------------
# TEXTS (DE + ES Erklärung mit richtiger Reihenfolge)
# ----------------------------

START_TEXT_DE = f"""✅ {BOT_NAME} online.

✅ /set Reihenfolge:
Auto / Modell / PLZ / Preis / BJ

📌 Beispiel:
 /set Audi a6 67157 10000 bj2010

Bedeutung:
- Audi a6  = Auto + Modell
- 67157    = PLZ
- 10000    = Max Preis (€)
- bj2010   = Mindest Baujahr (ab 2010)

⏱️ Der Bot überprüft jede Minute und schickt ALLE passenden Treffer als Link.
⚠️ Limit: maximal {MAX_PER_RUN} Links pro Minute (damit Telegram nicht ausrastet).

Befehle:
/set Audi a6 67157 10000 bj2010
/list
/del 1
/stop
"""

START_TEXT_ES = f"""✅ {BOT_NAME} online.

✅ Orden de /set:
Coche / Modelo / Código postal / Precio / Año (BJ)

📌 Ejemplo:
 /set Audi a6 67157 10000 bj2010

Significado:
- Audi a6  = coche + modelo
- 67157    = código postal
- 10000    = precio máximo (€)
- bj2010   = año mínimo (desde 2010)

⏱️ El bot revisa cada minuto y manda TODOS los resultados que coinciden como enlaces.
⚠️ Límite: máximo {MAX_PER_RUN} enlaces por minuto (para evitar spam).

Comandos:
/set Audi a6 67157 10000 bj2010
/list
/del 1
/stop
"""


# ----------------------------
# TELEGRAM COMMANDS
# ----------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT_DE)
    await update.message.reply_text(START_TEXT_ES)


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parsed = parse_set_args(update.message.text or "")

    if not parsed:
        await update.message.reply_text(
            "❌ Falsches Format!\n\n"
            "✅ Reihenfolge:\nAuto / Modell / PLZ / Preis / BJ\n\n"
            "Beispiel:\n/set Audi a6 67157 10000 bj2010"
        )
        return

    store = load_data()

    entry = {
        "chat_id": update.effective_chat.id,
        "query": parsed["query"],
        "plz": parsed["plz"],
        "price_to": parsed["price_to"],
        "radius_km": parsed["radius_km"],
        "year_from": parsed["year_from"],
        "only_photos": parsed["only_photos"],
    }

    store["searches"].append(entry)
    save_data(store)

    mobile_url = build_mobile_url(entry["query"], entry["price_to"], entry["plz"], entry["radius_km"], entry["year_from"])
    klein_url = build_kleinanzeigen_url(entry["query"], entry["price_to"])

    reply = (
        "✅ Suche gespeichert!\n\n"
        f"🚗 {entry['query']}\n"
        f"📍 PLZ: {entry['plz']} ({entry['radius_km']} km)\n"
        f"💶 Max Preis: {entry['price_to']}€\n"
    )
    if entry["year_from"]:
        reply += f"📅 ab Baujahr: {entry['year_from']}\n"

    reply += "\n🔎 Kleinanzeigen:\n" + klein_url + "\n\n"
    reply += "🔎 mobile.de:\n" + mobile_url + "\n\n"
    reply += f"⏱️ Check: jede Minute | Limit: {MAX_PER_RUN} Treffer/Minute"

    await update.message.reply_text(reply)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store = load_data()
    chat_id = update.effective_chat.id

    items = [s for s in store.get("searches", []) if s["chat_id"] == chat_id]
    if not items:
        await update.message.reply_text("Keine gespeicherten Suchen.")
        return

    lines = ["📋 Deine gespeicherten Suchen:\n"]
    for i, s in enumerate(items, start=1):
        bj = f" bj{s['year_from']}" if s.get("year_from") else ""
        lines.append(f"{i}) {s['query']} | {s['plz']} | {s['price_to']}€{bj}")

    await update.message.reply_text("\n".join(lines))


async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store = load_data()
    chat_id = update.effective_chat.id

    parts = (update.message.text or "").strip().split()
    if len(parts) < 2:
        await update.message.reply_text("Benutzung: /del 1")
        return

    idx = parse_int(parts[1])
    if not idx:
        await update.message.reply_text("Ungültige Nummer.")
        return

    items = [s for s in store.get("searches", []) if s["chat_id"] == chat_id]

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

    before = len(store.get("searches", []))
    store["searches"] = [s for s in store.get("searches", []) if s["chat_id"] != chat_id]
    after = len(store.get("searches", []))

    save_data(store)

    await update.message.reply_text(f"⛔️ Alle Suchen gelöscht. ({before - after} entfernt)")


# ----------------------------
# ALERT JOB (sendet ALLES was passt, Limit 10)
# ----------------------------

async def alert_job(context: ContextTypes.DEFAULT_TYPE):
    store = load_data()
    searches = store.get("searches", [])

    if not searches:
        return

    for s in searches:
        query = s["query"]
        price_to = s["price_to"]
        plz = s["plz"]
        radius = s.get("radius_km", DEFAULT_RADIUS_KM)
        year_from = s.get("year_from")
        only_photos = s.get("only_photos", True)

        mobile_url = build_mobile_url(query, price_to, plz, radius, year_from)
        klein_url = build_kleinanzeigen_url(query, price_to)

        mobile_results = fetch_mobile_results(mobile_url)
        klein_results = fetch_kleinanzeigen_results(klein_url)

        results = mobile_results + klein_results
        results = apply_filters(results, only_photos=only_photos)

        # ✅ ALLES schicken, aber Limit = 10 pro Minute
        hits = results[:MAX_PER_RUN]

        for hit in hits:
            msg = (
                f"🚨 Treffer!\n"
                f"🌍 {hit['source']}\n"
                f"🚗 {hit['title']}\n"
                f"🔗 {hit['url']}"
            )
            try:
                await context.bot.send_message(chat_id=s["chat_id"], text=msg)
            except Exception as e:
                logger.warning(f"send_message error: {e}")


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

    # ✅ jede Minute prüfen
    app.job_queue.run_repeating(alert_job, interval=CHECK_INTERVAL_SECONDS, first=10)

    logger.info("Bot gestartet...")
    app.run_polling()


if __name__ == "__main__":
    main()
