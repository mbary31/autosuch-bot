# AutoSuchBot – Email Forwarder

Diese Version scrapet **nicht** mobile.de / Kleinanzeigen (weil die auf Railway/Datacenter IPs oft blocken),
sondern liest **E-Mail Benachrichtigungen** deiner gespeicherten Suchen und postet neue Links in Telegram.

## Railway ENV
- TELEGRAM_BOT_TOKEN
- IMAP_HOST (z.B. imap.gmail.com)
- IMAP_PORT (optional, 993)
- IMAP_USER
- IMAP_PASS (App-Passwort)
- IMAP_FOLDER (optional, INBOX)
- CHECK_INTERVAL_SECONDS (optional, 180)
- MAX_LINKS_PER_RUN (optional, 10)

## Commands
- /start
- /status
- /run
