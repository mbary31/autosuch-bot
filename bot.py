import os, re, json, time, hashlib, traceback
from urllib.parse import urlencode, quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN (Railway Variables)")

DATA_DIR = "./data"
SEARCHES_FILE = f"{DATA_DIR}/searches.json"
SEEN_FILE = f"{DATA_DIR}/seen.json"
STATE_FILE = f"{DATA_DIR}/state.json"

CHECK_INTERVAL_SECONDS = 60
RESEND_AFTER_SECONDS = 2 * 60 * 60
MAX_SEND_PER_RUN = 10

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "keep-alive",
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

def state_get():
    return load_json(STATE_FILE, {"last_check": 0, "last_error": ""})

def state_set(**kwargs):
    s = state_get()
    s.update(kwargs)
    save_json(STATE_FILE, s)

def fetch(url: str, timeout: int = 25) -> tuple[int, str]:
    r = SESSION.get(url, timeout=timeout)
    return r.status_code, r.text

def dedupe_keep_order(items):
    seen = set()
    out = []
    for x in items:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out

# -------------------------
# URL BUILDER (MINIMAL)
# -------------------------

def kleinanzeigen_search_url(q: str, plz: str, radius_km: int, max_price: int) -> str:
    params = {
        "keywords": q,                # NUR "audi a5"
        "locationStr": plz,
        "radius": str(radius_km),
        "priceTo": str(max_price),
        "sortingField": "SORTING_DATE",
    }
    return "https://www.kleinanzeigen.de/s-autos/k0c216?" + urlencode(params, quote_via=quote_plus)

def mobile_search_url(q: str, plz: str, radius_km: int, max_price: int, min_year: int) -> str:
    params = {
        "isSearchRequest": "true",
        "vc": "Car",
        "q": q,                               # "audi a5"
        "zip": plz,
        "rad": str(radius_km),
        "maxPrice": str(max_price),
        "minFirstRegistrationDate": str(min_year),
        "sb": "rel",
    }
    return "https://suchen.mobile.de/fahrzeuge/search.html?" + urlencode(params, quote_via=quote_plus)

# -------------------------
# LINK EXTRACT
# -------------------------

def extract_kleinanzeigen_links(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    links = []

    # Kleinanzeigen: /s-anzeige/...
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

    # Variante 1: klassische Details-Links
    for a in soup.select('a[href*="/fahrzeuge/details.html"]'):
        href = a.get("href", "")
        if "details.html" not in href:
            continue
        full = urljoin("https://suchen.mobile.de", href.split("#")[0])
        links.append(full)

    # Variante 2: manchmal sind Links relativ ohne Domain
    for a in soup.find_all("a"):
        href = a.get("href", "") or ""
        if "/fahrzeuge/details.html" in href:
            full = urljoin("https://suchen.mobile.de", href.split("#")[0])
            links.append(full)

    return dedupe_keep_order(links)

# -------------------------
# SEEN / DEDUP
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

HELP_DE = (
    "✅ AutoSuchBot\n\n"
    "Format:\n"
    "/set MARKE MODELL PLZ UMKREIS_KM MAX_PREIS MIN_BJ\n\n"
    "Beispiel:\n"
    "/set audi a5 67157 400 13000 2010\n\n"
    "Andere:\n"
    "/list\n"
    "/del 1\n"
    "/stop\n"
    "/status\n"
)

HELP_ES = (
    "✅ AutoSearchBot\n\n"
    "Formato:\n"
    "/set MARCA MODELO CP RADIO_KM PRECIO_MAX AÑO_MIN\n\n"
    "Ejemplo:\n"
    "/set audi a5 67157 400 13000 2010\n\n"
    "Otros:\n"
    "/list\n"
    "/del 1\n"
    "/stop\n"
    "/status\n"
)

def parse_set(args: list[str]):
    if len(args) < 6:
        return None
    make = args[0].strip()
    model = args[1].strip()
    plz = re.sub(r"\D", "", args[2].strip())
    try:
        radius = int(args[3])
        max_price = int(args[4])
        min_year = int(re.sub(r"\D", "", args[5]))
    except:
        return None

    q = f"{make} {model}".strip().lower()
    return {"q": q, "plz": plz, "radius": radius, "max_price": max_price, "min_year": min_year, "created_at": time.time()}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_DE + "\n" + HELP_ES)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_DE + "\n" + HELP_ES)

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_data_dir()
    searches = load_searches()
    state = state_get()
    chat_id = str(update.effective_chat.id)
    count = len(searches.get(chat_id, []))
    last = state.get("last_check", 0)
    last_err = state.get("last_error", "")
    txt = (
        f"🧠 Status\n"
        f"- Gespeicherte Suchen: {count}\n"
        f"- Letzter Check: {time.ctime(last) if last else 'nie'}\n"
        f"- Letzter Fehler: {last_err[:300] if last_err else 'kein'}"
    )
    await update.message.reply_text(txt)

async def set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_data_dir()
    cfg = parse_set(context.args)
    if not cfg:
        await update.message.reply_text("❌ Falsches Format.\n" + HELP_DE)
        return

    searches = load_searches()
    chat_id = str(update.effective_chat.id)
    searches.setdefault(chat_id, [])
    searches[chat_id].append(cfg)
    save_searches(searches)

    await update.message.reply_text("✅ Suche gespeichert! Prüfe sofort…")
    await run_checks_for_chat(chat_id, context.application, verbose=True)

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

async def run_checks_for_chat(chat_id: str, app: Application, verbose: bool = False):
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
        diag = []

        # Kleinanzeigen
        kz_url = kleinanzeigen_search_url(q, plz, radius, max_price)
        try:
            code, html = fetch(kz_url)
            diag.append(f"KZ {code}")
            if code == 200:
                urls += extract_kleinanzeigen_links(html)
            else:
                diag.append("KZ blocked?")
        except Exception as e:
            diag.append(f"KZ err: {type(e).__name__}")

        # mobile.de
        mo_url = mobile_search_url(q, plz, radius, max_price, min_year)
        try:
            code, html = fetch(mo_url)
            diag.append(f"MO {code}")
            if code == 200:
                urls += extract_mobile_links(html)
            else:
                diag.append("MO blocked?")
        except Exception as e:
            diag.append(f"MO err: {type(e).__name__}")

        urls = dedupe_keep_order(urls)

        to_send = []
        for u in urls:
            if should_send(chat_seen, u):
                to_send.append(u)
                if len(to_send) >= MAX_SEND_PER_RUN:
                    break

        if to_send:
            msg = "\n".join(to_send)
            await app.bot.send_message(chat_id=int(chat_id), text=msg, disable_web_page_preview=False)
            for u in to_send:
                mark_sent(chat_seen, u)
        else:
            if verbose:
                await app.bot.send_message(
                    chat_id=int(chat_id),
                    text=f"ℹ️ Keine neuen Links gerade.\n({', '.join(diag)})"
                )

    save_seen(seen)

async def scheduled_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        ensure_data_dir()
        state_set(last_check=time.time(), last_error="")
        searches = load_searches()
        for chat_id in list(searches.keys()):
            await run_checks_for_chat(chat_id, context.application, verbose=False)
    except Exception:
        err = traceback.format_exc()
        print(err)
        state_set(last_error=err[:800])

def main():
    ensure_data_dir()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("set", set_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("del", del_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))

    app.job_queue.run_repeating(scheduled_job, interval=CHECK_INTERVAL_SECONDS, first=5)
    app.run_polling()

if __name__ == "__main__":
    main()
