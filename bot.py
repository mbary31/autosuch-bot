import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from openai import OpenAI

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

SYSTEM_PROMPT = """
Du bist ein Parser für Auto-Suchbefehle.
Gib NUR JSON zurück.

Eingabeformat:
set/<marke> <modell> <max_preis> <standort> <umkreis_km>

Beispiel:
set/audi a6 6000 Wachenheim an der Weinstraße 500

Antwortformat:
{
  "command": "set",
  "brand": "",
  "model": "",
  "max_price": 0,
  "location": "",
  "radius_km": 0,
  "search_urls": {
    "ebay_kleinanzeigen": "",
    "mobile_de": ""
  }
}
"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Ich bin online.\n\nBeispiel:\n/set audi a6 6000 Wachenheim an der Weinstraße 500"
    )

async def set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    args = raw.replace("/set", "").strip()

    if not args:
        await update.message.reply_text("Format:\n/set <marke> <modell> <max_preis> <standort> <umkreis_km>")
        return

    user_input = f"set/{args}"

    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_input},
        ],
    )

    content = resp.choices[0].message.content.strip()
    await update.message.reply_text(content)

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("set", set_cmd))
    app.run_polling()

if __name__ == "__main__":
    main()
