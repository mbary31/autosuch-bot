import os
import json
import time
import hashlib
import urllib.parse
import requests

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from openai import OpenAI

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

DATA_FILE = "data.json"
CHECK_INTERVAL_SECONDS = 180  # alle 3 Minuten


# -------------------------
# Helpers
# -------------------------
def load_data():
    if not os.path.exists(DATA_FILE):
        return {"users": {}}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def build_links(brand, model, max_price, location, radius_km):
    query = f"{brand} {model}".strip()
    q = urllib.parse.quote(query)

    # Kleinanzeigen: als Suchseite (am Anfang reicht das)
    kleinanzeigen_url = (
        f"https://www.kleinanzeigen.de/s-autos/k0c216?keywords={q}&priceTo={max_price}"
    )

    # mobile.de: einfache Suche (geht gut)
    mobile_url = (
        "https://suchen.mobile.de/fahrzeuge/search.html"
        f"?isSearchRequest=true&maxPrice={max_price}"
        f"&radius={radius_km}&ref=quickSearch"
        f"&vc=Car"
        f"&dam=0"
        f"&sb=rel"
        f"&makeModelVariant1.makeId={urllib.parse.quote(brand)}"
        f"&makeModelVariant1.modelDescription={urllib.parse.quote(model)}"
        f"&cn=DE"
    )

    return kleinanzeigen_url, mobile_url


def fetch_kleinanzeigen_titles(search_url):
    """
    Minimaler Check ohne API: liest HTML und greift nach Titeln.
    Nicht perfekt, aber funktioniert als 'first working version'.
    """
    headers = {
        "User-Agent": "Mozilla/5.0"
    }
    r = requests.get(search_url, headers=headers, timeout=15)
    html = r.text

    # sehr simple Titel-Erkennung (kann man später verbessern)
    titles = []
    for line in html.splitlines():
        if 'class="text-module-begin"' in line or 'class="ellipsis"' in line:
            # Not very reliable, but ok for MVP
            pass

    # besser: such nach typischem Titel-Muster
    # Kleinanzeigen hat oft: <h2 class="text-module-begin">TITLE</h2>
    import re
    matches = re.findall(r'<h2[^>]*class="text-module-begin"[^>]*>(.*?)</h2>', html)
    for m in matches[:10]:
        t = re.sub("<.*?>", "", m).strip()
        if t:
            titles.append(t)

    # fallback: wenn nix gefunden wird
    return titles[:10]


def make_id(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


SYSTEM_PROMPT = """
Du bist ein Parser für Auto-Suchbefehle.
Gib NUR JSON zurück.

Eingabeformat:
set/<marke> <modell> <max_preis> <standort> <umkreis_km>

Beispiel:
set/audi a6 6000 Wachenheim an der Weinstraße 500

Antwortformat:
{
  "brand": "",
  "model": "",
  "max_price": 0,
  "location": "",
  "radius_km": 0
}
"""


# -------------------------
# Telegram Commands
# -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ AutoSuchBot online.\n\n"
        "Benutzung:\n"
        "/set audi a6 6000 Wachenheim an der Weinstraße 500\n\n"
        "Andere:\n"
        "/list\n"
        "/del 1\n"
        "/stop"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Commands:\n"
        "/set <marke> <modell> <preis> <ort> <km>\n"
        "/list\n"
        "/del <nummer>\n"
        "/stop"
    )

async def set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    raw = update.message.text.strip()
    args = raw.replace("/set", "").strip()

    if not args:
        await update.message.reply_text("❌ Format:\n/set audi a6 6000 Berlin 200")
        return

    user_input = f"set/{args}"

    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_input},
        ],
    )

    parsed = json.loads(resp.choices[0].message.content)

    brand = parsed["brand"]
    model = parsed["model"]
    max_price = int(parsed["max_price"])
    location = parsed["location"]
    radius_km = int(parsed["radius_km"])

    kleinanzeigen_url, mobile_url = build_links(brand, model, max_price, location, radius_km)

    data = load_data()
    if user_id not in data["users"]:
        data["users"][user_id] = {"searches": [], "seen": []}

    new_search = {
        "brand": brand,
        "model": model,
        "max_price": max_price,
        "location": location,
        "radius_km": radius_km,
        "kleinanzeigen_url": kleinanzeigen_url,
        "mobile_url": mobile_url,
        "created": int(time.time())
    }

    data["users"][user_id]["searches"].append(new_search)
    save_data(data)

    await update.message.reply_text(
        "✅ Suche gespeichert!\n\n"
        f"🚗 {brand} {model}\n"
        f"💶 bis {max_price}€\n"
        f"📍 {location} ({radius_km} km)\n\n"
        f"🔎 Kleinanzeigen:\n{kleinanzeigen_url}\n\n"
        f"🔎 mobile.de:\n{mobile_url}\n\n"
        "⏱️ Alerts laufen jetzt automatisch."
    )

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()

    if user_id not in data["users"] or not data["users"][user_id]["searches"]:
        await update.message.reply_text("Keine gespeicherten Suchen.")
        return

    msg = "📌 Deine Suchen:\n\n"
    for i, s in enumerate(data["users"][user_id]["searches"], start=1):
        msg += (
            f"{i}) {s['brand']} {s['model']} bis {s['max_price']}€ | "
            f"{s['location']} ({s['radius_km']}km)\n"
        )

    await update.message.reply_text(msg)

async def del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()

    if user_id not in data["users"] or not data["users"][user_id]["searches"]:
        await update.message.reply_text("❌ Keine Suchen vorhanden.")
        return

    raw = update.message.text.strip()
    parts = raw.split()

    if len(parts) != 2 or not parts[1].isdigit():
        await update.message.reply_text("❌ Nutzung: /del 1")
        return

    idx = int(parts[1]) - 1

    if idx < 0 or idx >= len(data["users"][user_id]["searches"]):
        await update.message.reply_text("❌ Nummer nicht gültig.")
        return

    removed = data["users"][user_id]["searches"].pop(idx)
    save_data(data)

    await update.message.reply_text(f"✅ Gelöscht: {removed['brand']} {removed['model']}")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()

    if user_id not in data["users"]:
        await update.message.reply_text("Nix zu stoppen.")
        return

    data["users"][user_id]["searches"] = []
    save_data(data)

    await update.message.reply_text("🛑 Alle Alerts gestoppt und Suchen gelöscht.")


# -------------------------
# Background Job (Alerts)
# -------------------------
async def alert_job(context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    bot = context.bot

    for user_id, udata in data["users"].items():
        searches = udata.get("searches", [])
        seen = set(udata.get("seen", []))

        for s in searches:
            try:
                titles = fetch_kleinanzeigen_titles(s["kleinanzeigen_url"])
                for t in titles:
                    tid = make_id(t)
                    if tid not in seen:
                        seen.add(tid)
                        await bot.send_message(
                            chat_id=int(user_id),
                            text=f"🆕 Neue Anzeige gefunden:\n{t}\n\n🔎 Link:\n{s['kleinanzeigen_url']}"
                        )
            except Exception:
                # ignoriere Fehler, damit job nicht stirbt
                pass

        udata["seen"] = list(seen)

    save_data(data)


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("set", set_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("del", del_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))

    # Alle X Sekunden checken
    app.job_queue.run_repeating(alert_job, interval=CHECK_INTERVAL_SECONDS, first=20)

    app.run_polling()


if __name__ == "__main__":
    main()
