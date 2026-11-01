import os
import json
import asyncio
from datetime import datetime
from flask import Flask, request
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
import gspread
from oauth2client.service_account import ServiceAccountCredentials

app = Flask(__name__)

# Konfigurasi Token dan Channel ID dari Environment Variables
TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0"))

# --- KONFIGURASI GOOGLE SHEETS ---
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

def get_google_sheet():
    """Fungsi untuk menghubungkan bot ke Google Sheets."""
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    
    if GOOGLE_CREDENTIALS_JSON:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    else:
        creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
        
    client = gspread.authorize(creds)
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
            sheet = get_google_sheet()
            # Cek apakah message_id sudah ada untuk menghindari duplikat
            cells = sheet.findall(str(message_id))
            if not cells:
                sheet.append_row([
                    str(message_id),
                    file_id,
                    caption,
                    timestamp
                ])
                print(f"Sukses mencatat video ke Google Sheets! Message ID: {message_id}")
        except Exception as e:
            print(f"Gagal mencatat ke Google Sheets: {e}")

async def sync_spreadsheet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Perintah /sync untuk memeriksa dan membersihkan data jika video dihapus di channel."""
    message = update.message
    if not message:
        return

    await message.reply_text("🔄 Sedang memeriksa dan menyinkronkan data spreadsheet dengan channel...")

    try:
        sheet = get_google_sheet()
        # Ambil semua data di kolom pertama (Message ID) mulai baris ke-2
        message_ids = sheet.col_values(1)[1:] 
        
        removed_count = 0
        # Telusuri dari baris paling bawah ke atas agar indeks baris tidak bergeser saat dihapus
        for index in range(len(message_ids), 0, -1):
            msg_id_str = message_ids[index - 1]
            try:
                msg_id = int(msg_id_str)
                # Cek apakah pesan masih ada di channel menggunakan Telegram API
                # forwardMessage atau getChat memberikan indikasi apakah pesan masih eksis
                try:
                    # Mencoba meneruskan pesan ke chat bot/admin untuk verifikasi eksistensi
                    await context.bot.forward_message(
                        chat_id=message.chat_id,
                        from_chat_id=CHANNEL_ID,
                        message_id=msg_id
                    )
                    # Jika berhasil diteruskan (dan langsung dihapus lagi agar tidak mengganggu), berarti pesan masih ada
                    # (Atau kita biarkan jika ingin sekalian mengecek)
                except Exception:
                    # Jika Telegram mengembalikan error (pesan tidak ditemukan / sudah dihapus), hapus baris di Sheets
                    sheet.delete_rows(index + 1) # +1 karena index spreadsheet mulai dari 1 dan ada header
                    removed_count += 1
            except ValueError:
                continue

        await message.reply_text(f"✅ Sinkronisasi selesai! {removed_count} data baris yang videonya sudah dihapus berhasil dibersihkan dari spreadsheet.")
    except Exception as e:
        await message.reply_text(f"❌ Terjadi kesalahan saat sinkronisasi: {e}")

# Daftarkan handler
telegram_app.add_handler(MessageHandler(
    filters.Chat(chat_id=CHANNEL_ID) & (filters.VIDEO | filters.Document.VIDEO),
    auto_capture_channel_post
))
telegram_app.add_handler(CommandHandler("sync", sync_spreadsheet_command))

@app.route("/", methods=["GET"])
def index():
    return "Bot Telegram & Google Sheets Berjalan (Dengan Fitur Sync)", 200

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    """Endpoint webhook untuk menerima data dari Telegram."""
    if request.method == "POST":
        json_data = request.get_json(force=True)
        update = Update.de_json(json_data, telegram_app.bot)
        
        async def process():
            await telegram_app.initialize()
            await telegram_app.process_update(update)
            
        asyncio.run(process())
        
    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
