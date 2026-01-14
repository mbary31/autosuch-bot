import os
import re
import json
import time
import hashlib
import logging
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------------------------
# CONFIG
# ---------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing env var TELEGRAM_BOT_TOKEN")

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))  # every minute
MAX_RESULTS_PER_CHECK = int(os.getenv("MAX_RESULTS_PER_CHECK", "10"))    # limit per run
RESEND_AFTER_SECONDS = int(os.getenv("RESEND_AFTER_SECONDS", str(2 * 60 * 60)))  # 2h

# If min_year is set, we require a real year from detail page (no guessing)
REQUIRE_TRUSTED_YEAR = True

# blacklist words (auto-K.O.)
BLACKLIST = {
    "unfall",
    "bastler",
    "motorschaden",
    "defekt",
    "export",
    "teileträger",
    "teiletraeger",
    "ohne tüv",
    "ohne tuv",
}

REQUIRE_IMAGES = True

DATA_DIR = os.getenv("DATA_DIR", ".")
SEARCHES_FILE = os.path.join(DATA_DIR, "searches.json")
SENT_FILE = os.path.join(DATA_DIR, "sent_cache.json")

UA = os.getenv(
    "SCRAPE_UA",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("autosuch-bot")


# ---------------------------
# DATA
# ---------------------------

@dataclass
class Search:
    make: str
    model: str
    plz: str
    radius_km: int
    max_price: int
    min_year: int

    @property
    def query(self) -> str:
        return f"{self.make} {self.model}".strip()

    @property
    def key(self) -> str:
        return f"{self.make.lower()}|{self.model.lower()}|{self.plz}|{self.radius_km}|{self.max_price}|{self.min_year}"


SentCache = Dict[str, Dict[str, Dict[str, str]]]
SearchStore = Dict[str, List[Dict]]


# ---------------------------
# UTIL
# ---------------------------

def load_json(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log.warning("Failed to load %s: %s", path, e)
    return default


def save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def contains_blacklist(text: str) -> Optional[str]:
    t = norm(text)
    for w in BLACKLIST:
        if w in t:
            return w
    return None


def now_ts() -> int:
    return int(time.time())


def parse_price_eur(text: str) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"(\d[\d\.\s]*)\s?€", text)
    if not m:
        m = re.search(r"(\d[\d\.\s]*)", text)
    if not m:
        return None
    try:
        return int(m.group(1).replace(".", "").replace(" ", ""))
    except:
        return None


def pick_year_from_text(text: str) -> Optional[int]:
    # trusted-ish year extraction: must be 1950-2029
    if not text:
        return None
    m = re.search(r"\b(19[5-9]\d|20[0-2]\d)\b", text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except:
        return None


def hash_listing(title: str, price: str, location: str, year: str, extra: str = "") -> str:
    h = hashlib.sha256()
    h.update((title or "").encode("utf-8"))
    h.update(b"|")
    h.update((price or "").encode("utf-8"))
    h.update(b"|")
    h.update((location or "").encode("utf-8"))
    h.update(b"|")
    h.update((year or "").encode("utf-8"))
    h.update(b"|")
    h.update((extra or "").encode("utf-8"))
    return h.hexdigest()


# ---------------------------
# HTTP
# ---------------------------

session = requests.Session()
session.headers.update({
    "User-Agent": UA,
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"
})


def get_html(url: str) -> str:
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return r.text


# ---------------------------
# URL BUILDERS
# ---------------------------

def kleinanzeigen_search_url(s: Search) -> str:
    # IMPORTANT: no "autos," prefix. Only the actual query.
    params = {
        "keywords": s.query,
        "locationStr": s.plz,
        "radius": str(s.radius_km),
        "priceTo": str(s.max_price),
        "sortierung": "date",
    }
    return "https://www.kleinanzeigen.de/s-autos/k0c216?" + urlencode(params)


def mobile_search_url(s: Search) -> str:
    params = {
        "isSearchRequest": "true",
        "q": s.query,
        "maxPrice": str(s.max_price),
        "radius": str(s.radius_km),
        "zip": s.plz,
        "vc": "Car",
        "cn": "DE",
    }
    return "https://suchen.mobile.de/fahrzeuge/search.html?" + urlencode(params)


# ---------------------------
# DETAIL PARSERS (trusted year/price)
# ---------------------------

def kleinanzeigen_detail(url: str) -> Dict:
    html = get_html(url)
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)

    # title
    title = ""
    h1 = soup.select_one("h1")
    if h1:
        title = h1.get_text(" ", strip=True)

    # price
    price_text = ""
    p = soup.select_one("[id='viewad-price'], .boxedarticle--price")
    if p:
        price_text = p.get_text(" ", strip=True)

    # location
    location_text = ""
    loc = soup.select_one("#viewad-locality, .boxedarticle--details--full")
    if loc:
        location_text = loc.get_text(" ", strip=True)

    # year: Kleinanzeigen often has "Baujahr" in attributes
    year = None
    # try structured attribute rows
    for row in soup.select(".addetailslist--detail, .aditem-main--middle--description"):
        t = row.get_text(" ", strip=True)
        if "Baujahr" in t:
            y = pick_year_from_text(t)
            if y:
                year = y
                break
    if not year:
        # fallback: scan whole page for "Baujahr"
        m = re.search(r"Baujahr\s*[:\-]?\s*(19[5-9]\d|20[0-2]\d)", text)
        if m:
            year = int(m.group(1))

    # images
    has_img = bool(soup.select_one("img"))  # best effort

    return {
        "title": title,
        "price_text": price_text,
        "location_text": location_text,
        "year": year,
        "has_img": has_img,
        "raw_text": text[:2000],
    }


def mobile_detail(url: str) -> Dict:
    html = get_html(url)
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)

    # title
    title = ""
    h1 = soup.select_one("h1")
    if h1:
        title = h1.get_text(" ", strip=True)

    # price: mobile has lots of variants
    price_text = ""
    # try common price containers
    for sel in [
        "[data-testid='prime-price']",
        ".price-block__price",
        ".vehicle-prices__price",
        ".vip-price",
        ".g-col-6 .h3",  # fallback-ish
    ]:
        el = soup.select_one(sel)
        if el:
            price_text = el.get_text(" ", strip=True)
            if "€" in price_text or parse_price_eur(price_text) is not None:
                break

    # year: usually "Erstzulassung" or "Baujahr"
    year = None
    m = re.search(r"(Erstzulassung|Baujahr)\s*[:\-]?\s*(19[5-9]\d|20[0-2]\d)", text)
    if m:
        year = int(m.group(2))

    # images
    has_img = bool(soup.select_one("img"))

    return {
        "title": title,
        "price_text": price_text,
        "location_text": "",
        "year": year,
        "has_img": has_img,
        "raw_text": text[:2000],
    }


# ---------------------------
# SEARCH SCRAPERS (collect URLs)
# ---------------------------

def scrape_kleinanzeigen_urls(s: Search) -> List[Dict]:
    url = kleinanzeigen_search_url(s)
    soup = BeautifulSoup(get_html(url), "lxml")
    out = []
    for a in soup.select("a[href*='/s-anzeige/']"):
        href = a.get("href") or ""
        if not href:
            continue
        if not href.startswith("http"):
            href = urljoin("https://www.kleinanzeigen.de", href)

        out.append({
            "source": "kleinanzeigen",
            "id": href,
            "url": href,
        })
    # de-dupe while keeping order
    seen = set()
    uniq = []
    for x in out:
        if x["id"] in seen:
            continue
        seen.add(x["id"])
        uniq.append(x)
    return uniq


def scrape_mobile_urls(s: Search) -> List[Dict]:
    url = mobile_search_url(s)
    soup = BeautifulSoup(get_html(url), "lxml")

    candidates = []

    # multiple selectors (mobile changes a lot)
    selectors = [
        "a[href*='/auto-inserat/']",
        "a[data-testid='result-title']",
        "a[data-testid='vehicle-result-link']",
        "a[href*='link.mobile.de/']",
        "a[href*='www.mobile.de/']",
    ]

    for sel in selectors:
        for a in soup.select(sel):
            href = a.get("href") or ""
            if not href:
                continue
            if href.startswith("/"):
                href = urljoin("https://suchen.mobile.de", href)

            host = urlparse(href).netloc.lower()
            if ("mobile.de" not in host) and ("link.mobile.de" not in host):
                continue

            # only keep real car listings if possible
            if "/auto-inserat/" not in href and "link.mobile.de" not in host:
                continue

            candidates.append(href)

    # de-dupe while keeping order
    seen = set()
    uniq = []
    for href in candidates:
        if href in seen:
            continue
        seen.add(href)
        uniq.append({
            "source": "mobile",
            "id": href,
            "url": href,
        })
    return uniq


# ---------------------------
# FILTERS
# ---------------------------

def strict_match_make_model(s: Search, title: str, raw: str) -> bool:
    t = norm((title or "") + " " + (raw or ""))
    return (norm(s.make) in t) and (norm(s.model) in t)


def passes_filters(s: Search, item: Dict) -> Tuple[bool, str]:
    title = item.get("title", "")
    raw = item.get("raw_text", "")

    # strict make/model on mobile (stops “all Audis”)
    if item.get("source") == "mobile":
        if not strict_match_make_model(s, title, raw):
            return False, "no_strict_make_model"

    bad = contains_blacklist(title + " " + raw)
    if bad:
        return False, f"blacklist:{bad}"

    if REQUIRE_IMAGES and not item.get("has_img", False):
        return False, "no_image"

    p = parse_price_eur(item.get("price_text", ""))
    if p is not None and p > s.max_price:
        return False, "over_price"

    y = item.get("year")
    if s.min_year and REQUIRE_TRUSTED_YEAR:
        # If we require trusted year and we can't find one -> ignore (no wrong years)
        if y is None:
            return False, "no_trusted_year"
        if y < s.min_year:
            return False, "year_too_old"

    return True, "ok"


# ---------------------------
# DEDUP / SEND
# ---------------------------

def should_send(chat_id: str, item: Dict, sent_cache: SentCache) -> bool:
    listing_id = item["id"]
    title = item.get("title", "")
    price = item.get("price_text", "")
    loc = item.get("location_text", "")
    year = str(item.get("year") or "")

    content_hash = hash_listing(title, price, loc, year, item.get("raw_text", "")[:400])

    sent_cache.setdefault(chat_id, {})
    prev = sent_cache[chat_id].get(listing_id)
    ts = now_ts()

    if not prev:
        sent_cache[chat_id][listing_id] = {"ts": str(ts), "hash": content_hash}
        return True

    prev_ts = int(prev.get("ts", "0"))
    prev_hash = prev.get("hash", "")

    changed = (content_hash != prev_hash)
    too_old = (ts - prev_ts) >= RESEND_AFTER_SECONDS

    if changed or too_old:
        sent_cache[chat_id][listing_id] = {"ts": str(ts), "hash": content_hash}
        return True

    return False


def format_alert(item: Dict) -> str:
    src = item.get("source", "")
    title = (item.get("title") or "").strip()
    price = (item.get("price_text") or "").strip()
    loc = (item.get("location_text") or "").strip()
    year = item.get("year")
    url = (item.get("url") or "").strip()

    meta = []
    if year:
        meta.append(f"📆 BJ {year}")
    if price:
        meta.append(f"💶 {price}")
    if loc:
        meta.append(f"📍 {loc}")
    meta.append(f"🔎 {src}")

    return "\n".join([
        f"🚗 *{title}*",
        " | ".join(meta),
        f"🔗 {url}",
    ])


# ---------------------------
# TELEGRAM COMMANDS
# ---------------------------

def parse_set_args(text: str) -> Search:
    # /set Auto Model PLZ Umkreis Preis BJ
    parts = text.strip().split()
    if len(parts) < 7:
        raise ValueError("Format: /set Auto Model PLZ Umkreis Preis BJ (z.B. /set audi a6 67157 300 10000 2010)")

    make = parts[1]
    model = parts[2]
    plz = parts[3]

    if not re.fullmatch(r"\d{5}", plz):
        raise ValueError("PLZ muss 5-stellig sein (z.B. 67157).")

    try:
        radius = int(re.sub(r"\D", "", parts[4]))
        max_price = int(re.sub(r"\D", "", parts[5]))
        min_year = int(re.sub(r"\D", "", parts[6]))
    except:
        raise ValueError("Umkreis/Preis/BJ müssen Zahlen sein. Beispiel: /set audi a6 67157 300 10000 2010")

    return Search(make=make, model=model, plz=plz, radius_km=radius, max_price=max_price, min_year=min_year)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "✅ *AutoSuchBot online.*\n\n"
        "*Benutzung:*\n"
        "`/set Auto Model PLZ Umkreis Preis BJ`\n"
        "Beispiel:\n"
        "`/set audi a6 67157 300 10000 2010`\n\n"
        "*Andere:*\n"
        "`/list`  – gespeicherte Suchen\n"
        "`/del <nr>` – Suche löschen\n"
        "`/stop`  – alles löschen\n"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    try:
        s = parse_set_args(update.message.text)
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")
        return

    store: SearchStore = load_json("searches.json", {})
    store.setdefault(chat_id, [])
    store[chat_id].append(asdict(s))
    save_json("searches.json", store)

    ka = kleinanzeigen_search_url(s)
    mo = mobile_search_url(s)

    await update.message.reply_text(
        "✅ *Suche gespeichert!*\n"
        f"🚗 `{s.make} {s.model}`\n"
        f"📍 PLZ `{s.plz}` + `{s.radius_km} km`\n"
        f"💶 bis `{s.max_price}€`\n"
        f"📆 ab BJ `{s.min_year}`\n\n"
        f"🔎 *Kleinanzeigen Suche:*\n{ka}\n\n"
        f"🔎 *mobile.de Suche:*\n{mo}\n\n"
        "⏱️ Alerts laufen jetzt automatisch (jede Minute).",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    store: SearchStore = load_json("searches.json", {})
    items = store.get(chat_id, [])
    if not items:
        await update.message.reply_text("Keine gespeicherten Suchen.")
        return

    lines = ["📌 *Deine Suchen:*"]
    for i, d in enumerate(items, start=1):
        s = Search(**d)
        lines.append(
            f"{i}) 🚗 `{s.make} {s.model}` | 📍 `{s.plz}` + `{s.radius_km}km` | 💶 `{s.max_price}€` | 📆 `{s.min_year}`"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    store: SearchStore = load_json("searches.json", {})
    items = store.get(chat_id, [])
    if not items:
        await update.message.reply_text("Keine gespeicherten Suchen.")
        return

    parts = update.message.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await update.message.reply_text("Format: /del 1")
        return

    idx = int(parts[1]) - 1
    if idx < 0 or idx >= len(items):
        await update.message.reply_text("❌ Nummer existiert nicht. Nutze /list")
        return

    removed = items.pop(idx)
    store[chat_id] = items
    save_json("searches.json", store)

    s = Search(**removed)
    await update.message.reply_text(f"🗑️ Gelöscht: {s.make} {s.model} ({s.plz})")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    store: SearchStore = load_json("searches.json", {})
    store.pop(chat_id, None)
    save_json("searches.json", store)

    sent: SentCache = load_json("sent_cache.json", {})
    sent.pop(chat_id, None)
    save_json("sent_cache.json", sent)

    await update.message.reply_text("🛑 Alles gelöscht. Keine Alerts mehr.")


# ---------------------------
# CHECK LOOP
# ---------------------------

async def check_once(app: Application):
    store: SearchStore = load_json("searches.json", {})
    sent: SentCache = load_json("sent_cache.json", {})

    for chat_id, searches in store.items():
        compiled: List[Search] = []
        for d in searches:
            try:
                compiled.append(Search(**d))
            except:
                continue
        if not compiled:
            continue

        for s in compiled:
            # collect URLs
            items = []
            try:
                items += scrape_kleinanzeigen_urls(s)
            except Exception as e:
                log.warning("Kleinanzeigen URL scrape failed: %s", e)

            try:
                items += scrape_mobile_urls(s)
            except Exception as e:
                log.warning("mobile URL scrape failed: %s", e)

            sent_count = 0

            # enrich + filter + send (limit requests)
            for base in items[:200]:
                src = base["source"]
                url = base["url"]

                try:
                    if src == "kleinanzeigen":
                        details = kleinanzeigen_detail(url)
                    else:
                        details = mobile_detail(url)
                except Exception as e:
                    log.warning("detail fetch failed (%s): %s", src, e)
                    continue

                item = {**base, **details}

                ok, _reason = passes_filters(s, item)
                if not ok:
                    continue

                if not should_send(chat_id, item, sent):
                    continue

                text = format_alert(item)
                try:
                    await app.bot.send_message(
                        chat_id=int(chat_id),
                        text=text,
                        parse_mode=ParseMode.MARKDOWN,
                        disable_web_page_preview=False,
                    )
                    sent_count += 1
                except Exception as e:
                    log.warning("send_message failed: %s", e)

                if sent_count >= MAX_RESULTS_PER_CHECK:
                    break

    save_json("sent_cache.json", sent)


async def job_runner(context: ContextTypes.DEFAULT_TYPE):
    await check_once(context.application)


def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("stop", cmd_stop))

    app.job_queue.run_repeating(job_runner, interval=CHECK_INTERVAL_SECONDS, first=5)
    log.info("Bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
