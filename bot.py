import os, re, json, time, hashlib
from urllib.parse import urlencode, quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

DATA_DIR = "./data"
SEARCHES_FILE = f"{DATA_DIR}/searches.json"
SEEN_FILE = f"{DATA_DIR}/seen.json"

CHECK_INTERVAL_SECONDS = 60         # jede Minute
RESEND_AFTER_SECONDS = 2 * 60 * 60  # 2 Stunden
MAX_SEND_PER_RUN = 10               # pro Suche und Minute max 10 neue Links (sonst Spam)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
})

def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return default

def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def fetch(url: str, timeout: int = 20) -> str:
    r = SESSION.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text

# -------------------------
# URL BUILDER (MINIMAL)
# -------------------------

def kleinanzeigen_search_url(q: str, plz: str, radius_km: int, max_price: int) -> str:
    # Autos Kategorie k0c216
    params = {
        "keywords": q,              # NUR "audi a5" -> kein "auto," Mist
        "locationStr": plz,
        "radius": str(radius_km),
        "priceTo": str(max_price),
        "s": "SORTING_DATE",        # sort by newest
    }
    return "https://www.kleinanzeigen.de/s-autos/k0c216?" + urlencode(params, quote_via=quote_plus)

def mobile_search_url(q: str, plz: str, radius_km: int, max_price: int, min_year: int) -> str:
    # Minimal wie es im Browser auch läuft – nix MakeId/ModelId Gedöns
    params = {
        "isSearchRequest": "true",
        "vc": "Car",
        "q": q,                            # "audi a5"
        "zip": plz,
        "rad": str(radius_km),
        "maxPrice": str(max_price),
        "minFirstRegistrationDate": str(min_year),
        "sb": "rel",                       # relevance/new-ish
    }
    return "https://suchen.mobile.de/fahrzeuge/search.html?" + urlencode(params, quote_via=quote_plus)

# -------------------------
# LINK EXTRACT (LIST ONLY)
# -------------------------

def extract_kleinanzeigen_links(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    links = []
    for a in soup.select('a[href^="/s-anzeige/"]'):
        href = a.get("href", "")
        if not href.startswith("/s-anzeige/"):
            continue
        full = urljoin("https://www.kleinanzeigen.de", href.split("#")[0])
        links.append(full)
    return dedupe_keep_order(links)

def extract_mobile_links(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    links = []

    # mobile list links usually contain /fahrzeuge/details.html?id=
    for a in soup.select('a[href*="/fahrzeuge/details.html"]'):
        href = a.get("href", "")
        if "id=" not in href:
            continue
        full = urljoin("https://suchen.mobile.de", href.split("#")[0])
        links.append(full)

    return dedupe_keep_order(links)

def dedupe_keep_order(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for x in items:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out

# -------------------------
# SEEN / DEDUP (per chat)
# -------------------------

def link_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8", errors="ignore")).hexdigest()

def should_send(chat_seen: dict, url: str) -> bool:
    k = link_key(url)
    now = time.time()
    if k not in chat_seen:
        return True
    last = chat_seen[k].get("last_sent", 0)
    return (now - last) >= RESEND_AFTER_SECONDS

def mark_sent(chat_seen: dict, url: str):
    k = link_key(url)
    chat_seen[k] = {"last_sent": time.time()}

# -------------------------
# SEARCH STORAGE
# -------------------------

def load_searches():
    return load_json(SEARCHES_FILE, {})

def save_searches(searches):
    save_json(SEARCHES_FILE, searches)

def load_seen():
    return load_json(SEEN_FILE, {})

def save_seen(seen):
    save_json(SEEN_FILE, seen)

# -------------------------
# TELEGRAM COMMANDS
# -------------------------

HELP_TEXT_DE = (
    "✅ AutoSuchBot\n\n"
    "Format:\n"
    "/set MARKE MODELL PLZ UMKREIS_KM MAX_PREIS MIN_BJ\n\n"
    "Beispiel:\n"
    "/set audi a5 67157 400 13000 2010\n\n"
    "Andere:\n"
    "/list\n"
    "/del 1\n"
    "/stop\n"
)

HELP_TEXT_ES = (
    "✅ AutoSearchBot\n\n"
    "Formato:\n"
    "/set MARCA MODELO CP RADIO_KM PRECIO_MAX AÑO_MIN\n\n"
    "Ejemplo:\n"
    "/set audi a5 67157 400 13000 2010\n\n"
    "Otros:\n"
    "/list\n"
    "/del 1\n"
    "/stop\n"
)

def parse_set(args: list[str]):
    # /set audi a5 67157 400 13000 2010
    if len(args) < 6:
        return None
    make = args[0].strip()
    model = args[1].strip()
    plz = args[2].strip()
    try:
        radius = int(args[3])
        max_price = int(args[4])
        min_year = int(re.sub(r"\D", "", args[5]))
    except:
        return None

    q = f"{make} {model}".strip().lower()
    return {"q": q, "plz": plz, "radius": radius, "max_price": max_price, "min_year": min_year, "created_at": time.time()}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT_DE + "\n" + HELP_TEXT_ES)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT_DE + "\n" + HELP_TEXT_ES)

async def set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_data_dir()
    cfg = parse_set(context.args)
    if not cfg:
        await update.message.reply_text("❌ Falsches Format.\n" + HELP_TEXT_DE)
        return

    searches = load_searches()
    chat_id = str(update.effective_chat.id)
    searches.setdefault(chat_id, [])
    searches[chat_id].append(cfg)
    save_searches(searches)

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
        lines.append(f"{i}) {s['q']} | {s['plz']} ({s['radius']}km) | <= {s['max_price']}€ | >= {s['min_year']}")
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

    lst.pop(idx)
    searches[chat_id] = lst
    save_searches(searches)
    await update.message.reply_text("🗑️ Gelöscht.")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_data_dir()
    searches = load_searches()
    chat_id = str(update.effective_chat.id)
    searches[chat_id] = []
    save_searches(searches)
    await update.message.reply_text("🛑 Alle Suchen gelöscht.")

# -------------------------
# CHECK LOOP
# -------------------------

async def run_checks_for_chat(chat_id: str, app: Application):
    ensure_data_dir()
    searches = load_searches()
    seen = load_seen()
    seen.setdefault(chat_id, {})
    chat_seen = seen[chat_id]

    cfgs = searches.get(chat_id, [])
    if not cfgs:
        return

    for cfg in cfgs:
        q = cfg["q"]
        plz = cfg["plz"]
        radius = int(cfg["radius"])
        max_price = int(cfg["max_price"])
        min_year = int(cfg["min_year"])

        urls: list[str] = []

        # Kleinanzeigen
        try:
            kz_url = kleinanzeigen_search_url(q, plz, radius, max_price)
            kz_html = fetch(kz_url)
            urls += extract_kleinanzeigen_links(kz_html)
        except:
            pass

        # mobile.de
        try:
            mo_url = mobile_search_url(q, plz, radius, max_price, min_year)
            mo_html = fetch(mo_url)
            urls += extract_mobile_links(mo_html)
        except:
            pass

        # Nur neue / wieder erlaubte Links schicken
        to_send = []
        for u in urls:
            if should_send(chat_seen, u):
                to_send.append(u)
                if len(to_send) >= MAX_SEND_PER_RUN:
                    break

        if to_send:
            # NUR LINKS. Sonst nix.
            msg = "\n".join(to_send)
            await app.bot.send_message(chat_id=int(chat_id), text=msg, disable_web_page_preview=False)

            for u in to_send:
                mark_sent(chat_seen, u)

    save_seen(seen)

async def scheduled_job(context: ContextTypes.DEFAULT_TYPE):
    ensure_data_dir()
    searches = load_searches()
    for chat_id in list(searches.keys()):
        await run_checks_for_chat(chat_id, context.application)

def main():
    ensure_data_dir()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("set", set_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("del", del_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))

    app.job_queue.run_repeating(scheduled_job, interval=CHECK_INTERVAL_SECONDS, first=5)
    app.run_polling()

if __name__ == "__main__":
    main()
