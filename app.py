import os
from flask import Flask, request
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

app = Flask(__name__)

# Ambil token dan konfigurasi dari environment variable (sangat penting untuk cloud hosting)
TOKEN = os.environ.get("BOT_TOKEN", "9037444926:AAGAzo0KDswH4DJuQWgZSab4Rn4Gga0UNfM")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "-1003971007907"))
# URL webhook publik Anda nantinya (misal dari Render: https://nama-app.onrender.com)
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://your-app-name.onrender.com")

# Inisialisasi Application telegram-bot
telegram_app = ApplicationBuilder().token(TOKEN).build()

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Logika saat video dikirim ke bot."""
    video = update.message.video or update.message.document
    if not video:
        await update.message.reply_text("Kirimkan file video yang valid!")
        return

    try:
        # Teruskan video ke Channel Private
        await context.bot.forward_message(
            chat_id=CHANNEL_ID,
            from_chat_id=update.effective_chat.id,
            message_id=update.message.message_id
        )
        file_id = video.file_id
        await update.message.reply_text(
            f"✅ **Video berhasil disimpan via Webhook!**\n\n🆔 File ID:\n`{file_id}`",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Gagal menyimpan: {e}")

# Tambahkan handler video
telegram_app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))

@app.route("/", methods=["GET"])
def index():
    return "Bot Telegram Webhook is running!", 200

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    """Endpoint yang akan dipanggil oleh Telegram setiap ada pesan masuk."""
    if request.method == "POST":
        json_data = request.get_json(force=True)
        update = Update.de_json(json_data, telegram_app.bot)
        
        # Jalankan update secara asynchronous di event loop bot
        import asyncio
        asyncio.run(telegram_app.process_update(update))
        
    return "OK", 200

if __name__ == "__main__":
    # Untuk testing lokal
    app.run(host="0.0.5.0", port=int(os.environ.get("PORT", 5000)))