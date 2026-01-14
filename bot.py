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
from telegram.ext import Application, CommandHandler, ContextTypes

# ----------------------------
# SETTINGS
# ----------------------------

BOT_NAME = "AutoSuchBot"
DATA_FILE = "search_data.json"

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))  # ✅ jede Minute
DEFAULT_RADIUS_KM = 300
DEFAULT_ONLY_WITH_PHOTOS = True

# ✅ Inserate dürfen alle 2h wiederholt werden
RESEND_AFTER_SECONDS = 2 * 60 * 60  # 2 Stunden

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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AutoSuchBot/2.0)"
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ----------------------------
# STORAGE
# ----------------------------

def load_data() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        return {"searches": [], "seen": {}}  # seen: {key: {hash,last_sent}}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "searches" not in data:
            data["searches"] = []
        if "seen" not in data:
            data["seen"] = {}
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

def parse_int(x: str) -> Optional[int]:
    try:
        digits = re.sub(r"[^\d]", "", x)
        return int(digits) if digits else None
    except Exception:
        return None

def contains_blacklist(text: str) -> bool:
    t = (text or "").lower()
    return any(w in t for w in BLACKLIST_WORDS)

def normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def parse_price_eur(text: str) -> Optional[int]:
    """
    Versucht Preis in Euro zu erkennen, z.B. "10.000 €", "9999€", "12.500 € VB".
    Gibt int (EUR) zurück, sonst None.
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
    # Wenn sich Titel oder Preis ändert → anderer Fingerprint → wird neu gesendet
    return sha16(f"{normalize_spaces(title).lower()}|{price}|{url}")

# ----------------------------
# /set Parser (Reihenfolge fix!)
# Auto/Model/PLZ/Preis/BJ
# Beispiel: /set Audi a6 67157 10000 bj2010
# ----------------------------

def parse_set_args(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    raw = re.sub(r"^/set(@\w+)?\s*", "", raw).strip()
    if not raw:
        return {}

    parts = raw.split()

    # BJ token "bj2010" optional
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

    # Mindest: query... PLZ Preis
    if len(parts) < 4:
        return {}

    plz = parts[-2]
    price = parts[-1]
    if not re.fullmatch(r"\d{5}", plz):
        return {}
    if not re.fullmatch(r"\d{3,8}", price):
        return {}

    query = " ".join(parts[:-2]).strip()
    if not query:
        return {}

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
        "radius": str(radius),
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
# FETCH RESULTS
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
    Robustere mobile-Extraktion:
    - findet detail links
    - versucht Preis in der Nähe zu lesen
    """
    out: List[Dict[str, Any]] = []
    html = fetch_html(url)
    if not html:
        return out

    soup = BeautifulSoup(html, "lxml")

    # mobile hat häufig Links zu details.html?id=...
    # Wir nehmen ALLE passenden links, dann dedupe.
    anchors = soup.select("a[href*='fahrzeuge/details.html']")
    seen = set()

    for a in anchors:
        href = (a.get("href") or "").strip()
        if "details.html" not in href:
            continue

        if href.startswith("/"):
            href = "https://suchen.mobile.de" + href

        # Nur echte detail links
        if "details.html?id=" not in href:
            continue

        if href in seen:
            continue
        seen.add(href)

        title = normalize_spaces(a.get_text(" ", strip=True)) or "mobile.de Anzeige"

        # Preis versuchen aus Eltern-Container zu ziehen
        container = a.find_parent()
        price = None
        if container:
            txt = normalize_spaces(container.get_text(" ", strip=True))
            price = parse_price_eur(txt)

        out.append({
            "source": "mobile.de",
            "title": title,
            "url": href,
            "text": (title).lower(),
            "has_photo": True,  # mobile hat praktisch immer Bilder
            "price": price,
        })

    return out

def fetch_kleinanzeigen_results(url: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    html = fetch_html(url)
    if not html:
        return out

    soup = BeautifulSoup(html, "lxml")

    # Kleinanzeigen: Ergebnis-Kacheln sind oft article/aditem – wir nehmen link heuristisch
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

        # Preis versuchen aus Parent-Text
        container = a.find_parent()
        price = None
        has_photo = True
        if container:
            txt = normalize_spaces(container.get_text(" ", strip=True))
            price = parse_price_eur(txt)
            # Bild check (best-effort)
            img = container.find("img")
            has_photo = img is not None

        out.append({
            "source": "Kleinanzeigen",
            "title": title,
            "url": href,
            "text": (title).lower(),
            "has_photo": has_photo,
            "price": price,
        })

    return out

# ----------------------------
# FILTER + SEND RULES
# ----------------------------

def passes_filters(item: Dict[str, Any], only_photos: bool, price_to: int) -> bool:
    # Blacklist
    if contains_blacklist(item.get("title", "") + " " + item.get("text", "")):
        return False

    # Fotos
    if only_photos and not item.get("has_photo", False):
        return False

    # Preis: wenn wir einen Preis erkannt haben, HART prüfen
    price = item.get("price")
    if price is not None and price_to is not None and price > price_to:
        return False

    return True

def should_send(seen: Dict[str, Any], key: str, new_hash: str, now: int) -> bool:
    """
    Regeln:
    - noch nie gesendet → senden
    - wenn sich Fingerprint ändert (Titel/Preis) → senden
    - sonst wenn älter als 2h → erneut senden
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
# TEXTS
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

⏱️ Check: jede Minute
✅ Keine Dopplungen
✅ Wenn Inserat sich ändert → wird erneut geschickt
✅ Wiederholung nach 2 Stunden erlaubt
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

⏱️ Revisión: cada minuto
✅ Sin duplicados
✅ Si el anuncio cambia → se envía otra vez
✅ Repetición permitida cada 2 horas
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
    reply += "⏱️ Check: jede Minute | Dopplungen: nein | Wiederholung: 2h | Änderungen: sofort"

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
    # auch seen löschen (optional), damit bei Neustart wieder alles neu kommen kann
    # -> wir lassen seen drin, damit es sauber bleibt
    save_data(store)

    await update.message.reply_text("⛔️ Alle Suchen gelöscht (Alerts aus).")

# ----------------------------
# ALERT JOB
# ----------------------------

async def alert_job(context: ContextTypes.DEFAULT_TYPE):
    store = load_data()
    searches = store.get("searches", [])
    seen: Dict[str, Any] = store.get("seen", {})

    if not searches:
        return

    now = int(time.time())

    for s in searches:
        query = s["query"]
        price_to = int(s["price_to"])
        plz = s["plz"]
        radius = int(s.get("radius_km", DEFAULT_RADIUS_KM))
        year_from = s.get("year_from")  # aktuell nur im mobile-link berücksichtigt
        only_photos = bool(s.get("only_photos", True))

        mobile_url = build_mobile_url(query, price_to, plz, radius, year_from)
        klein_url = build_kleinanzeigen_url(query, price_to)

        # fetch
        mobile_results = fetch_mobile_results(mobile_url)
        klein_results = fetch_kleinanzeigen_results(klein_url)

        results = mobile_results + klein_results

        # Filter + dedupe/resend logic
        for item in results:
            if not passes_filters(item, only_photos=only_photos, price_to=price_to):
                continue

            # Key pro Anzeige (Quelle+URL) -> stabil
            key = f"{item['source']}|{item['url']}"
            new_hash = fingerprint(item.get("title", ""), item.get("price"), item.get("url", ""))

            if not should_send(seen, key, new_hash, now):
                continue

            # senden
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

    # speichern (seen kann groß werden -> optional trim)
    # wir trimmen auf ~50k Einträge, damit Datei nicht explodiert
    if len(seen) > 50000:
        # sortiere nach last_sent und behalte neueste 50k
        items_sorted = sorted(seen.items(), key=lambda kv: int(kv[1].get("last_sent", 0)), reverse=True)[:50000]
        seen = dict(items_sorted)

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
