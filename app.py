import os
import json
from datetime import datetime
from flask import Flask, request
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
import gspread
from oauth2client.service_account import ServiceAccountCredentials

app = Flask(__name__)

# Konfigurasi Token dan Channel ID dari Environment Variables
TOKEN = os.environ.get("BOT_TOKEN", "9037444926:AAGAzo0KDswH4DJuQWgZSab4Rn4Gga0UNfM")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "-1003971007907"))

# --- KONFIGURASI GOOGLE SHEETS ---
# Render membaca credentials JSON dari Environment Variable (agar aman)
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

def get_google_sheet():
    """Fungsi untuk menghubungkan bot ke Google Sheets."""
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    
    if GOOGLE_CREDENTIALS_JSON:
        # Jika kredensial disimpan di Environment Variable Render
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    else:
        # Jika dijalankan lokal menggunakan file credentials.json
        creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
        
    client = gspread.authorize(creds)
    # Ganti "Database Video Bot" dengan nama Google Spreadsheet Anda
    sheet = client.open("Database Video Bot").sheet1 
    return sheet

# Inisialisasi aplikasi Telegram bot
telegram_app = ApplicationBuilder().token(TOKEN).build()

async def auto_capture_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fungsi otomatis saat ada video di-upload ke channel, lalu dicatat ke Sheets."""
    post = update.channel_post
    if not post or post.chat.id != CHANNEL_ID:
        return

    video = post.video or post.document
    caption = post.caption or "Tanpa Keterangan"
    message_id = post.message_id
    
    if video:
        file_id = video.file_id
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            # Masukkan data otomatis ke Google Sheets
            sheet = get_google_sheet()
            # Kolom: Message ID, File ID, Caption, Tanggal
            sheet.append_row([
                str(message_id),
                file_id,
                caption,
                timestamp
            ])
            print(f"✅ Sukses mencatat video ke Google Sheets! Message ID: {message_id}")
        except Exception as e:
            print(f"❌ Gagal mencatat ke Google Sheets: {e}")

# Daftarkan handler pemantau channel
telegram_app.add_handler(MessageHandler(
    filters.Chat(chat_id=CHANNEL_ID) & (filters.VIDEO | filters.Document.VIDEO),
    auto_capture_channel_post
))

@app.route("/", methods=["GET"])
def index():
    return "Bot Telegram & Google Sheets Berjalan!", 200

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    """Endpoint webhook untuk menerima data dari Telegram."""
    if request.method == "POST":
        json_data = request.get_json(force=True)
        update = Update.de_json(json_data, telegram_app.bot)
        
        import asyncio
        asyncio.run(telegram_app.process_update(update))
        
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
