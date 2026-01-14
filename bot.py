import os
import re
import json
import time
import hashlib
import logging
from typing import Dict, Any, List, Optional

import requests
from bs4 import BeautifulSoup

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ----------------------------
# SETTINGS
# ----------------------------

BOT_NAME = "AutoSuchBot"
DATA_FILE = "search_data.json"

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))  # jede Minute
RESEND_AFTER_SECONDS = 2 * 60 * 60  # 2 Stunden

DEFAULT_ONLY_WITH_PHOTOS = True

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

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AutoSuchBot/3.0)"}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ----------------------------
# STORAGE
# ----------------------------

def load_data() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        return {"searches": [], "seen": {}}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("searches", [])
        data.setdefault("seen", {})
        return data
    except Exception:
        return {"searches": [], "seen": {}}

def save_data(data: Dict[str, Any]) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def sha16(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]

# ----------------------------
# HELPERS
# ----------------------------

def normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def contains_blacklist(text: str) -> bool:
    t = (text or "").lower()
    return any(w in t for w in BLACKLIST_WORDS)

def parse_price_eur(text: str) -> Optional[int]:
    """
    Erkenne Preise wie "10.000 €", "9999€", "12 500 €".
    """
    if not text:
        return None
    t = text.replace("\xa0", " ").replace(".", "").replace("€", " €")
    m = re.search(r"(\d{1,3}(?:\s?\d{3})*|\d+)\s*€", t)
    if not m:
        return None
    num = m.group(1).replace(" ", "")
    try:
        return int(num)
    except Exception:
        return None

def fingerprint(title: str, price: Optional[int], url: str) -> str:
    return sha16(f"{normalize_spaces(title).lower()}|{price}|{url}")

def should_send(seen: Dict[str, Any], key: str, new_hash: str, now: int) -> bool:
    """
    - nie gesendet -> senden
    - geändert -> senden
    - sonst nach 2h wiederholen
    """
    rec = seen.get(key)
    if not rec:
        return True
    old_hash = rec.get("hash")
    last_sent = int(rec.get("last_sent", 0))
    if old_hash != new_hash:
        return True
    if now - last_sent >= RESEND_AFTER_SECONDS:
        return True
    return False

def mark_sent(seen: Dict[str, Any], key: str, new_hash: str, now: int) -> None:
    seen[key] = {"hash": new_hash, "last_sent": now}

# ----------------------------
# /set Parser
# Format: Auto/Model PLZ Umkreis Preis BJ
# Beispiel: /set Audi a6 67157 300 10000 bj2010
# ----------------------------

def parse_set_args(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    raw = re.sub(r"^/set(@\w+)?\s*", "", raw).strip()
    if not raw:
        return {}

    parts = raw.split()

    # optional bjYYYY
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

    # Mindest: query... PLZ RADIUS PRICE
    if len(parts) < 5:
        return {}

    plz = parts[-3]
    radius = parts[-2]
    price = parts[-1]

    if not re.fullmatch(r"\d{5}", plz):
        return {}
    if not re.fullmatch(r"\d{1,4}", radius):
        return {}
    if not re.fullmatch(r"\d{3,8}", price):
        return {}

    query = " ".join(parts[:-3]).strip()
    if not query:
        return {}

    return {
        "query": query.lower(),
        "plz": plz,
        "radius_km": int(radius),
        "price_to": int(price),
        "year_from": year_from,
        "only_photos": DEFAULT_ONLY_WITH_PHOTOS,
    }

# ----------------------------
# SEARCH URLS
# ----------------------------

def build_mobile_url(query: str, price_to: int, plz: str, radius_km: int, year_from: Optional[int]) -> str:
    base = "https://suchen.mobile.de/fahrzeuge/search.html"
    params = {
        "isSearchRequest": "true",
        "vc": "Car",
        "dam": "0",
        "sb": "rel",
        "cn": "DE",
        "zipcode": plz,
        "radius": str(radius_km),
        "maxPrice": str(price_to),
        "q": query,
    }
    if year_from:
        params["minFirstRegistrationDate"] = str(year_from)

    q = "&".join([f"{k}={requests.utils.quote(str(v))}" for k, v in params.items()])
    return f"{base}?{q}"

def build_kleinanzeigen_url(query: str, price_to: int) -> str:
    # Kleinanzeigen: wenigstens keywords + max price sauber (Radius via PLZ ist ohne Region-ID nicht zuverlässig)
    base = "https://www.kleinanzeigen.de/s-autos/k0c216"
    q = requests.utils.quote(query)
    return f"{base}?keywords={q}&priceTo={price_to}"

# ----------------------------
# FETCH
# ----------------------------

def fetch_html(url: str) -> Optional[str]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code != 200:
            return None
        return r.text
    except Exception as e:
        logger.warning(f"fetch_html error: {e}")
        return None

def fetch_mobile_results(url: str) -> List[Dict[str, Any]]:
    """
    mobile HTML ändert öfter. Wir nehmen breite Heuristik:
    - alle links auf details.html?id=
    - Preis aus Umfeld-Text best-effort
    """
    out: List[Dict[str, Any]] = []
    html = fetch_html(url)
    if not html:
        return out

    soup = BeautifulSoup(html, "lxml")
    anchors = soup.select("a[href*='fahrzeuge/details.html?id=']")
    seen = set()

    for a in anchors:
        href = (a.get("href") or "").strip()
        if not href:
            continue
        if href.startswith("/"):
            href = "https://suchen.mobile.de" + href
        if "details.html?id=" not in href:
            continue
        href = href.split("&")[0]

        if href in seen:
            continue
        seen.add(href)

        title = normalize_spaces(a.get_text(" ", strip=True)) or "mobile.de Anzeige"

        # Preis aus Parent-Text
        price = None
        container = a.find_parent()
        if container:
            txt = normalize_spaces(container.get_text(" ", strip=True))
            price = parse_price_eur(txt)

        out.append({
            "source": "mobile.de",
            "title": title,
            "url": href,
            "text": title.lower(),
            "has_photo": True,
            "price": price,
        })

    return out

def fetch_kleinanzeigen_results(url: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    html = fetch_html(url)
    if not html:
        return out

    soup = BeautifulSoup(html, "lxml")
    anchors = soup.select("a[href*='/s-anzeige/']")
    seen = set()

    for a in anchors:
        href = (a.get("href") or "").strip()
        if "/s-anzeige/" not in href:
            continue
        if href.startswith("/"):
            href = "https://www.kleinanzeigen.de" + href
        href = href.split("?")[0]

        if href in seen:
            continue
        seen.add(href)

        title = normalize_spaces(a.get_text(" ", strip=True)) or "Kleinanzeigen Anzeige"

        price = None
        has_photo = True
        container = a.find_parent()
        if container:
            txt = normalize_spaces(container.get_text(" ", strip=True))
            price = parse_price_eur(txt)
            img = container.find("img")
            has_photo = img is not None

        out.append({
            "source": "Kleinanzeigen",
            "title": title,
            "url": href,
            "text": title.lower(),
            "has_photo": has_photo,
            "price": price,
        })

    return out

# ----------------------------
# FILTER
# ----------------------------

def passes_filters(item: Dict[str, Any], only_photos: bool, price_to: int) -> bool:
    if contains_blacklist(item.get("title", "") + " " + item.get("text", "")):
        return False
    if only_photos and not item.get("has_photo", False):
        return False

    # Preis hart filtern, wenn erkannt
    price = item.get("price")
    if price is not None and price > price_to:
        return False

    return True

# ----------------------------
# Telegram Texts
# ----------------------------

START_TEXT_DE = f"""✅ {BOT_NAME} online.

✅ /set Reihenfolge:
Auto/Modell  PLZ  Umkreis  Preis  BJ

📌 Beispiel:
 /set Audi a6 67157 300 10000 bj2010

⏱️ Check: jede Minute
✅ Keine Dopplungen
✅ Wenn Inserat sich ändert -> sofort nochmal
✅ Wiederholung nach 2 Stunden erlaubt
"""

START_TEXT_ES = f"""✅ {BOT_NAME} online.

✅ Orden de /set:
Coche/Modelo  CP  Radio  Precio  Año (BJ)

📌 Ejemplo:
 /set Audi a6 67157 300 10000 bj2010

⏱️ Revisión: cada minuto
✅ Sin duplicados
✅ Si el anuncio cambia -> se envía otra vez
✅ Repetición cada 2 horas
"""

# ----------------------------
# Commands
# ----------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT_DE)
    await update.message.reply_text(START_TEXT_ES)

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
        lines.append(f"{i}) {s['query']} | {s['plz']} | {s['radius_km']}km | {s['price_to']}€{bj}")
    await update.message.reply_text("\n".join(lines))

async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store = load_data()
    chat_id = update.effective_chat.id

    parts = (update.message.text or "").strip().split()
    if len(parts) < 2:
        await update.message.reply_text("Benutzung: /del 1")
        return

    try:
        idx = int(parts[1])
    except Exception:
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
    store["searches"] = [s for s in store.get("searches", []) if s["chat_id"] != chat_id]
    save_data(store)
    await update.message.reply_text("⛔️ Alle Suchen gelöscht (Alerts aus).")

async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parsed = parse_set_args(update.message.text or "")
    if not parsed:
        await update.message.reply_text(
            "❌ Falsches Format!\n\n"
            "✅ Reihenfolge:\nAuto/Modell PLZ Umkreis Preis BJ\n\n"
            "Beispiel:\n/set Audi a6 67157 300 10000 bj2010"
        )
        return

    store = load_data()
    entry = {
        "chat_id": update.effective_chat.id,
        "query": parsed["query"],
        "plz": parsed["plz"],
        "radius_km": parsed["radius_km"],
        "price_to": parsed["price_to"],
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
    reply += "Jetzt schicke ich dir einmal ALLE aktuellen Treffer. Danach nur neu/geändert + Wiederholung nach 2h."

    await update.message.reply_text(reply)

    # ✅ Initial Dump: sofort alles Aktuelle schicken (ohne doppelt im Loop zu landen)
    await send_all_current_hits_once(context, entry)

# ----------------------------
# Sending logic
# ----------------------------

async def send_all_current_hits_once(context: ContextTypes.DEFAULT_TYPE, s: Dict[str, Any]) -> None:
    """
    Holt einmal die aktuellen Treffer und sendet sie.
    Markiert sie als "seen", damit der Minutely-Job nicht sofort wieder alles doppelt ballert.
    """
    store = load_data()
    seen: Dict[str, Any] = store.get("seen", {})
    now = int(time.time())

    mobile_url = build_mobile_url(s["query"], s["price_to"], s["plz"], s["radius_km"], s.get("year_from"))
    klein_url = build_kleinanzeigen_url(s["query"], s["price_to"])

    mobile_results = fetch_mobile_results(mobile_url)
    klein_results = fetch_kleinanzeigen_results(klein_url)

    results = mobile_results + klein_results

    sent_count = 0
    for item in results:
        if not passes_filters(item, only_photos=s.get("only_photos", True), price_to=int(s["price_to"])):
            continue

        key = f"{item['source']}|{item['url']}"
        new_hash = fingerprint(item.get("title", ""), item.get("price"), item.get("url", ""))

        # beim initial dump: IMMER senden, aber auch als seen markieren
        price_txt = f"{item['price']}€" if item.get("price") is not None else "Preis ?"
        msg = (
            f"🚨 Treffer (Initial)!\n"
            f"🌍 {item['source']}\n"
            f"🚗 {item.get('title','')}\n"
            f"💶 {price_txt}\n"
            f"🔗 {item['url']}"
        )
        await context.bot.send_message(chat_id=s["chat_id"], text=msg)
        mark_sent(seen, key, new_hash, now)
        sent_count += 1

    store["seen"] = seen
    save_data(store)

    await context.bot.send_message(chat_id=s["chat_id"], text=f"✅ Initial-Dump fertig: {sent_count} Treffer gesendet.")

# ----------------------------
# ALERT JOB (jede Minute)
# ----------------------------

async def alert_job(context: ContextTypes.DEFAULT_TYPE):
    store = load_data()
    searches = store.get("searches", [])
    seen: Dict[str, Any] = store.get("seen", {})
    now = int(time.time())

    if not searches:
        return

    for s in searches:
        mobile_url = build_mobile_url(s["query"], int(s["price_to"]), s["plz"], int(s["radius_km"]), s.get("year_from"))
        klein_url = build_kleinanzeigen_url(s["query"], int(s["price_to"]))

        mobile_results = fetch_mobile_results(mobile_url)
        klein_results = fetch_kleinanzeigen_results(klein_url)

        results = mobile_results + klein_results

        for item in results:
            if not passes_filters(item, only_photos=s.get("only_photos", True), price_to=int(s["price_to"])):
                continue

            key = f"{item['source']}|{item['url']}"
            new_hash = fingerprint(item.get("title", ""), item.get("price"), item.get("url", ""))

            if not should_send(seen, key, new_hash, now):
                continue

            price_txt = f"{item['price']}€" if item.get("price") is not None else "Preis ?"
            msg = (
                f"🚨 Treffer!\n"
                f"🌍 {item['source']}\n"
                f"🚗 {item.get('title','')}\n"
                f"💶 {price_txt}\n"
                f"🔗 {item['url']}"
            )
            try:
                await context.bot.send_message(chat_id=s["chat_id"], text=msg)
                mark_sent(seen, key, new_hash, now)
            except Exception as e:
                logger.warning(f"send_message error: {e}")

    store["seen"] = seen
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

    app.job_queue.run_repeating(alert_job, interval=CHECK_INTERVAL_SECONDS, first=10)

    logger.info("Bot gestartet...")
    app.run_polling()

if __name__ == "__main__":
    main()
