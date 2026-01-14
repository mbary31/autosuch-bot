import os
import re
import json
import time
import hashlib
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode, quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# =========================
# CONFIG
# =========================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Missing env var TELEGRAM_BOT_TOKEN")

CHECK_INTERVAL_SECONDS = 60  # jede Minute

DATA_DIR = "./data"
SEARCHES_FILE = os.path.join(DATA_DIR, "searches.json")
SEEN_FILE = os.path.join(DATA_DIR, "seen.json")

# Wenn Anzeige schon geschickt wurde:
RESEND_AFTER_SECONDS = 2 * 60 * 60  # 2h

# Blacklist (Punkt 3) – kannst du hier fix ändern
DEFAULT_BLACKLIST = [
    "unfall",
    "bastler",
    "motorschaden",
    "defekt",
    "export",
    "teileträger",
    "ohne tüv",
    "ohne tüv!",
    "ohne tüv.",
]

# Nur mit Bildern (Punkt 1)
REQUIRE_PICTURES = True

# =========================
# DATA MODELS
# =========================

@dataclass
class SearchConfig:
    make: str
    model: str
    plz: str
    radius_km: int
    max_price_eur: int
    min_year: int
    created_at: float

    def query_text(self) -> str:
        # Wichtig: KEIN "auto," davor – nur Marke+Modell
        return f"{self.make} {self.model}".strip()


@dataclass
class Listing:
    site: str                # "kleinanzeigen" | "mobile"
    listing_id: str
    title: str
    price_eur: Optional[int]
    year: Optional[int]
    location: str
    url: str
    has_pictures: Optional[bool] = None

    def fingerprint(self) -> str:
        # Wenn sich Preis/Titel/Ort/Jahr ändert -> anderer Fingerprint -> erneut senden
        base = f"{self.title}|{self.price_eur}|{self.year}|{self.location}"
        return hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()


# =========================
# STORAGE HELPERS
# =========================

def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)

def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def load_searches() -> Dict[str, List[SearchConfig]]:
    raw = load_json(SEARCHES_FILE, {})
    out: Dict[str, List[SearchConfig]] = {}
    for chat_id, items in raw.items():
        out[chat_id] = [SearchConfig(**x) for x in items]
    return out

def save_searches(searches: Dict[str, List[SearchConfig]]):
    raw = {cid: [asdict(s) for s in lst] for cid, lst in searches.items()}
    save_json(SEARCHES_FILE, raw)

def load_seen() -> Dict[str, Dict[str, dict]]:
    # seen[chat_id][site:listing_id] = { "last_sent": ts, "fingerprint": fp }
    return load_json(SEEN_FILE, {})

def save_seen(seen: Dict[str, Dict[str, dict]]):
    save_json(SEEN_FILE, seen)


# =========================
# HTTP
# =========================

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
})

def fetch(url: str, timeout: int = 20) -> str:
    r = SESSION.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text


# =========================
# PARSING HELPERS
# =========================

def parse_price_eur(text: str) -> Optional[int]:
    if not text:
        return None
    t = text.strip().lower()
    if "vb" in t:
        # VB trotzdem Zahl holen
        pass
    # 10.000 € / 10000€ / 10 000 €
    nums = re.findall(r"(\d[\d\.\s]*)", t)
    if not nums:
        return None
    n = nums[0]
    n = n.replace(".", "").replace(" ", "")
    try:
        return int(n)
    except:
        return None

def contains_blacklist(title: str, blacklist: List[str]) -> bool:
    t = (title or "").lower()
    return any(w in t for w in blacklist)

def normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


# =========================
# KLEINANZEIGEN
# =========================

def build_kleinanzeigen_search_url(q: str, plz: str, radius_km: int, max_price: int) -> str:
    # Autos Kategorie ist k0c216 (wie du schon gesehen hast)
    params = {
        "keywords": q,
        "locationStr": plz,
        "radius": str(radius_km),
        "priceTo": str(max_price),
        "sortingField": "SORTING_DATE",
        "sortingOrder": "DESCENDING",
    }
    return "https://www.kleinanzeigen.de/s-autos/k0c216?" + urlencode(params, quote_via=quote_plus)

def parse_kleinanzeigen_listings(html: str) -> List[Tuple[str, str]]:
    """
    returns list of (listing_id, url)
    """
    soup = BeautifulSoup(html, "lxml")
    links = []
    for a in soup.select('a[href*="/s-anzeige/"]'):
        href = a.get("href", "")
        if not href.startswith("/s-anzeige/"):
            continue
        # id steckt oft am ende: .../1234567890-216-xxxx
        m = re.search(r"/s-anzeige/.*?/(\d{6,})-\d+-", href)
        listing_id = m.group(1) if m else href
        full = urljoin("https://www.kleinanzeigen.de", href)
        links.append((listing_id, full))
    # dedupe preserve order
    seen = set()
    out = []
    for lid, u in links:
        key = (lid, u)
        if key in seen:
            continue
        seen.add(key)
        out.append((lid, u))
    return out

def parse_kleinanzeigen_detail(url: str, listing_id: str) -> Optional[Listing]:
    html = fetch(url)
    soup = BeautifulSoup(html, "lxml")

    title = normalize_space(soup.select_one("h1").get_text(" ", strip=True) if soup.select_one("h1") else "")
    if not title:
        return None

    price_txt = ""
    price_el = soup.select_one('[data-testid="vip-price"]') or soup.select_one(".boxedarticle--price") or soup.select_one(".aditem-main--middle--price-shipping--price")
    if price_el:
        price_txt = normalize_space(price_el.get_text(" ", strip=True))
    price = parse_price_eur(price_txt)

    # Location
    loc = ""
    loc_el = soup.select_one('[data-testid="vip-location"]') or soup.select_one(".boxedarticle--details--text") or soup.select_one(".aditem-main--top--left")
    if loc_el:
        loc = normalize_space(loc_el.get_text(" ", strip=True))
    loc = loc[:80]

    # Has pictures: wenn irgendein Bild in Galerie vorhanden
    has_pics = bool(soup.select("img"))

    # Year: best effort – Kleinanzeigen ist wild, daher mehrere Versuche
    year = None
    text_all = soup.get_text("\n", strip=True).lower()

    # Häufig: "Erstzulassung" oder "Baujahr"
    m = re.search(r"(erstzulassung|baujahr)\s*[:\n]\s*(\d{4})", text_all, re.IGNORECASE)
    if m:
        try:
            year = int(m.group(2))
        except:
            year = None
    else:
        # Alternative: "EZ 04/2012" -> 2012
        m2 = re.search(r"\bez\s+\d{1,2}/(\d{4})\b", text_all, re.IGNORECASE)
        if m2:
            try:
                year = int(m2.group(1))
            except:
                year = None

    return Listing(
        site="kleinanzeigen",
        listing_id=str(listing_id),
        title=title,
        price_eur=price,
        year=year,
        location=loc or "—",
        url=url,
        has_pictures=has_pics,
    )

def kleinanzeigen_search(cfg: SearchConfig, limit: int = 20) -> List[Listing]:
    url = build_kleinanzeigen_search_url(cfg.query_text(), cfg.plz, cfg.radius_km, cfg.max_price_eur)
    html = fetch(url)
    pairs = parse_kleinanzeigen_listings(html)[:limit]

    listings: List[Listing] = []
    for lid, link in pairs:
        try:
            li = parse_kleinanzeigen_detail(link, lid)
            if li:
                listings.append(li)
        except Exception:
            continue
    return listings


# =========================
# MOBILE.DE (best-effort scraping)
# =========================

def build_mobile_search_url(make: str, model: str, plz: str, radius_km: int, max_price: int, min_year: int) -> str:
    # Keyword-Suche + Filter. Mobile ist zickig – aber das ist die stabilste "ohne API-key" Variante.
    # Der Trick: q=<make>+<model> statt nur model
    q = f"{make} {model}".strip()
    params = {
        "isSearchRequest": "true",
        "vc": "Car",
        "dam": "0",
        "sb": "rel",
        "ms": "",  # leave blank
        "lang": "de",
        "maxPrice": str(max_price),
        "minFirstRegistrationDate": str(min_year),  # EZ ab Jahr
        "rad": str(radius_km),
        "zip": plz,
        "q": q,
    }
    return "https://suchen.mobile.de/fahrzeuge/search.html?" + urlencode(params, quote_via=quote_plus)

def parse_mobile_listings(html: str) -> List[Tuple[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    # Links zu detail pages enthalten meist "/fahrzeuge/details.html?id="
    for a in soup.select('a[href*="/fahrzeuge/details.html"]'):
        href = a.get("href", "")
        if "id=" not in href:
            continue
        m = re.search(r"id=(\d+)", href)
        lid = m.group(1) if m else href
        full = urljoin("https://suchen.mobile.de", href)
        out.append((lid, full))
    # dedupe
    seen = set()
    res = []
    for lid, u in out:
        if (lid, u) in seen:
            continue
        seen.add((lid, u))
        res.append((lid, u))
    return res

def parse_mobile_detail(url: str, listing_id: str) -> Optional[Listing]:
    html = fetch(url)
    soup = BeautifulSoup(html, "lxml")

    title = normalize_space(soup.select_one("h1").get_text(" ", strip=True) if soup.select_one("h1") else "")
    if not title:
        return None

    # price
    price = None
    price_el = soup.select_one('[data-testid="price-label"]') or soup.select_one('[data-testid="price"]')
    if price_el:
        price = parse_price_eur(price_el.get_text(" ", strip=True))

    # year: often "Erstzulassung" somewhere
    year = None
    txt = soup.get_text("\n", strip=True)
    m = re.search(r"Erstzulassung\s*\n\s*([0-9]{2}/)?(\d{4})", txt, re.IGNORECASE)
    if m:
        try:
            year = int(m.group(2))
        except:
            year = None

    # location
    loc = ""
    loc_el = soup.select_one('[data-testid="seller-address"]') or soup.select_one('[data-testid="location"]')
    if loc_el:
        loc = normalize_space(loc_el.get_text(" ", strip=True))
    loc = loc[:80]

    # pics: details page usually has images if any
    has_pics = bool(soup.select("img"))

    return Listing(
        site="mobile",
        listing_id=str(listing_id),
        title=title,
        price_eur=price,
        year=year,
        location=loc or "—",
        url=url,
        has_pictures=has_pics,
    )

def mobile_search(cfg: SearchConfig, limit: int = 20) -> List[Listing]:
    url = build_mobile_search_url(cfg.make, cfg.model, cfg.plz, cfg.radius_km, cfg.max_price_eur, cfg.min_year)
    html = fetch(url)
    pairs = parse_mobile_listings(html)[:limit]

    listings: List[Listing] = []
    for lid, link in pairs:
        try:
            li = parse_mobile_detail(link, lid)
            if li:
                listings.append(li)
        except Exception:
            continue
    return listings


# =========================
# FILTERS + DEDUP
# =========================

def matches_filters(li: Listing, cfg: SearchConfig, blacklist: List[str]) -> bool:
    # Blacklist in title
    if contains_blacklist(li.title, blacklist):
        return False

    # pictures
    if REQUIRE_PICTURES and li.has_pictures is False:
        return False

    # price
    if li.price_eur is not None and li.price_eur > cfg.max_price_eur:
        return False

    # year
    if li.year is not None and li.year < cfg.min_year:
        return False

    return True

def should_send(chat_seen: Dict[str, dict], li: Listing) -> bool:
    key = f"{li.site}:{li.listing_id}"
    now = time.time()
    fp = li.fingerprint()

    if key not in chat_seen:
        return True

    last = chat_seen[key].get("last_sent", 0)
    old_fp = chat_seen[key].get("fingerprint", "")

    # wenn geändert -> sofort senden
    if old_fp != fp:
        return True

    # sonst erst nach 2h wieder
    if now - last >= RESEND_AFTER_SECONDS:
        return True

    return False

def mark_sent(chat_seen: Dict[str, dict], li: Listing):
    key = f"{li.site}:{li.listing_id}"
    chat_seen[key] = {
        "last_sent": time.time(),
        "fingerprint": li.fingerprint(),
    }

def format_listing(li: Listing) -> str:
    price = f"{li.price_eur} €" if li.price_eur is not None else "—"
    year = f"{li.year}" if li.year is not None else "—"
    site = "kleinanzeigen" if li.site == "kleinanzeigen" else "mobile.de"
    return (
        f"🚗 <b>{li.title}</b>\n"
        f"💶 <b>{price}</b> | 🗓️ <b>{year}</b>\n"
        f"📍 {li.location} | 🔎 <i>{site}</i>\n"
        f"🔗 {li.url}"
    )


# =========================
# TELEGRAM COMMANDS
# =========================

HELP_DE = (
    "✅ <b>AutoSuchBot</b>\n\n"
    "Format:\n"
    "<code>/set MARKE MODELL PLZ UMKREIS_KM MAX_PREIS MIN_BJ</code>\n\n"
    "Beispiel:\n"
    "<code>/set audi a6 67157 300 10000 2010</code>\n\n"
    "Andere:\n"
    "<code>/list</code>  – gespeicherte Suchen\n"
    "<code>/del 1</code> – Suche löschen\n"
    "<code>/stop</code> – alle Suchen löschen\n"
)

HELP_ES = (
    "✅ <b>AutoSearchBot</b>\n\n"
    "Formato:\n"
    "<code>/set MARCA MODELO CP RADIO_KM PRECIO_MAX AÑO_MIN</code>\n\n"
    "Ejemplo:\n"
    "<code>/set audi a6 67157 300 10000 2010</code>\n\n"
    "Otros:\n"
    "<code>/list</code>  – búsquedas guardadas\n"
    "<code>/del 1</code> – borrar búsqueda\n"
    "<code>/stop</code> – borrar todas\n"
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        HELP_DE + "\n" + HELP_ES,
        parse_mode=ParseMode.HTML
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        HELP_DE + "\n" + HELP_ES,
        parse_mode=ParseMode.HTML
    )

def parse_set_args(args: List[str]) -> Optional[SearchConfig]:
    # /set audi a6 67157 300 10000 2010
    if len(args) < 6:
        return None
    make = args[0].strip()
    model = args[1].strip()
    plz = args[2].strip()
    try:
        radius = int(args[3])
        max_price = int(args[4])
        min_year = int(re.sub(r"\D", "", args[5]))  # erlaubt bj2010 -> 2010
    except:
        return None

    if radius <= 0 or max_price <= 0 or min_year < 1900 or min_year > 2100:
        return None

    return SearchConfig(
        make=make,
        model=model,
        plz=plz,
        radius_km=radius,
        max_price_eur=max_price,
        min_year=min_year,
        created_at=time.time(),
    )

async def set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_data_dir()
    searches = load_searches()

    cfg = parse_set_args(context.args)
    if not cfg:
        await update.message.reply_text(
            "❌ Falsches Format.\n"
            "Nutze:\n"
            "<code>/set MARKE MODELL PLZ UMKREIS_KM MAX_PREIS MIN_BJ</code>\n"
            "Beispiel:\n"
            "<code>/set audi a6 67157 300 10000 2010</code>\n",
            parse_mode=ParseMode.HTML
        )
        return

    chat_id = str(update.effective_chat.id)
    searches.setdefault(chat_id, [])
    searches[chat_id].append(cfg)
    save_searches(searches)

    # Sofort einmal prüfen (damit du direkt Links siehst)
    await update.message.reply_text("✅ Suche gespeichert! Prüfe sofort…")
    await run_checks_for_chat(chat_id, context.application)

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_data_dir()
    searches = load_searches()
    chat_id = str(update.effective_chat.id)
    lst = searches.get(chat_id, [])
    if not lst:
        await update.message.reply_text("Keine gespeicherten Suchen.")
        return

    lines = []
    for i, s in enumerate(lst, start=1):
        lines.append(
            f"{i}) 🚗 {s.make} {s.model} | 📍 {s.plz} ({s.radius_km} km) | 💶 bis {s.max_price_eur}€ | 🗓️ ab {s.min_year}"
        )
    await update.message.reply_text("\n".join(lines))

async def del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_data_dir()
    searches = load_searches()
    chat_id = str(update.effective_chat.id)
    lst = searches.get(chat_id, [])

    if not context.args:
        await update.message.reply_text("Nutze: /del 1")
        return

    try:
        idx = int(context.args[0]) - 1
    except:
        await update.message.reply_text("Nutze: /del 1")
        return

    if idx < 0 or idx >= len(lst):
        await update.message.reply_text("❌ Diese Suche gibt’s nicht.")
        return

    removed = lst.pop(idx)
    searches[chat_id] = lst
    save_searches(searches)

    await update.message.reply_text(f"🗑️ Gelöscht: {removed.make} {removed.model} ({removed.plz})")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_data_dir()
    searches = load_searches()
    chat_id = str(update.effective_chat.id)
    searches[chat_id] = []
    save_searches(searches)
    await update.message.reply_text("🛑 Alle Suchen gelöscht.")


# =========================
# CHECK LOOP
# =========================

async def run_checks_for_chat(chat_id: str, app: Application):
    ensure_data_dir()
    searches = load_searches()
    seen = load_seen()

    cfgs = searches.get(chat_id, [])
    if not cfgs:
        return

    blacklist = DEFAULT_BLACKLIST
    seen.setdefault(chat_id, {})
    chat_seen = seen[chat_id]

    for cfg in cfgs:
        # Hol Listings (kleinanzeigen + mobile)
        results: List[Listing] = []

        try:
            results += kleinanzeigen_search(cfg, limit=25)
        except Exception:
            pass

        try:
            results += mobile_search(cfg, limit=25)
        except Exception:
            pass

        # Filter + sort newest-ish (keine echten timestamps, daher einfach Reihenfolge)
        matched: List[Listing] = []
        for li in results:
            if matches_filters(li, cfg, blacklist):
                matched.append(li)

        # Senden
        for li in matched:
            if should_send(chat_seen, li):
                await app.bot.send_message(
                    chat_id=int(chat_id),
                    text=format_listing(li),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False
                )
                mark_sent(chat_seen, li)

    save_seen(seen)

async def scheduled_job(context: ContextTypes.DEFAULT_TYPE):
    ensure_data_dir()
    searches = load_searches()
    for chat_id in list(searches.keys()):
        await run_checks_for_chat(chat_id, context.application)


# =========================
# MAIN
# =========================

def main():
    ensure_data_dir()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("set", set_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("del", del_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))

    # Job every minute
    app.job_queue.run_repeating(scheduled_job, interval=CHECK_INTERVAL_SECONDS, first=5)

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
