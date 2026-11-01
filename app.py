import os
import json
import hmac
import asyncio
import logging
import threading
from datetime import datetime

from flask import Flask, request, abort
from telegram import Update
from telegram.error import BadRequest, RetryAfter
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
import gspread

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Konfigurasi (Environment Variables)
# ---------------------------------------------------------------------------
TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0"))
# Secret acak untuk memverifikasi bahwa request webhook benar-benar dari Telegram
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
# Secret untuk endpoint /sync (dipanggil oleh cron)
SYNC_SECRET = os.environ.get("SYNC_SECRET")
# ID chat pribadi Anda dengan bot (kirim /start ke bot dulu). Dipakai untuk
# memeriksa apakah sebuah pesan channel masih ada.
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")
SHEET_NAME = os.environ.get("SHEET_NAME", "Database Video Bot")

if not TOKEN or not CHANNEL_ID:
    raise RuntimeError("BOT_TOKEN dan CHANNEL_ID wajib diisi.")
if not WEBHOOK_SECRET or len(WEBHOOK_SECRET) < 16:
    raise RuntimeError("WEBHOOK_SECRET wajib diisi (min. 16 karakter, hanya A-Z a-z 0-9 _ -).")

# ---------------------------------------------------------------------------
# Google Sheets (client di-cache, semua panggilan blocking dijalankan di thread)
# ---------------------------------------------------------------------------
_sheet_lock = threading.Lock()
_sheet = None


def get_google_sheet():
    global _sheet
    if _sheet is None:
        if GOOGLE_CREDENTIALS_JSON:
            client = gspread.service_account_from_dict(json.loads(GOOGLE_CREDENTIALS_JSON))
        else:
            client = gspread.service_account(filename="credentials.json")
        _sheet = client.open(SHEET_NAME).sheet1
    return _sheet


def sanitize_cell(value: str) -> str:
    """Cegah formula injection (=, +, -, @) pada isi sel dari caption."""
    value = str(value)
    if value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def record_video(message_id: int, file_id: str, caption: str) -> bool:
    """Catat video ke sheet. Return True jika baris baru ditambahkan."""
    with _sheet_lock:
        sheet = get_google_sheet()
        existing = sheet.col_values(1)  # kolom A = message_id, cocokkan persis
        if str(message_id) in existing:
            return False
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sheet.append_row(
            [str(message_id), file_id, sanitize_cell(caption[:1000]), timestamp],
            value_input_option="RAW",
        )
        return True


def delete_rows_by_message_ids(message_ids: set) -> int:
    with _sheet_lock:
        sheet = get_google_sheet()
        col = sheet.col_values(1)
        rows = [i + 1 for i, v in enumerate(col) if v in message_ids]
        for r in sorted(rows, reverse=True):  # hapus dari bawah agar indeks tidak bergeser
            sheet.delete_rows(r)
        return len(rows)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
telegram_app = ApplicationBuilder().token(TOKEN).concurrent_updates(True).build()


async def auto_capture_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = update.channel_post
    if not post or post.chat.id != CHANNEL_ID:
        return
    media = post.video or post.document
    if not media:
        return
    caption = post.caption or "Tanpa Keterangan"
    try:
        added = await asyncio.to_thread(record_video, post.message_id, media.file_id, caption)
        if added:
            log.info("Video dicatat. Message ID: %s", post.message_id)
    except Exception:
        log.exception("Gagal mencatat ke Google Sheets")


telegram_app.add_handler(
    MessageHandler(
        filters.UpdateType.CHANNEL_POST
        & filters.Chat(chat_id=CHANNEL_ID)
        & (filters.VIDEO | filters.Document.VIDEO),
        auto_capture_channel_post,
    )
)

# Satu event loop permanen di thread terpisah (menghindari asyncio.run per request
# yang membuat loop baru dan merusak koneksi httpx milik bot).
_loop = asyncio.new_event_loop()


def _run_loop():
    asyncio.set_event_loop(_loop)
    _loop.run_forever()


threading.Thread(target=_run_loop, daemon=True).start()


def run_async(coro, timeout=300):
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout)


run_async(telegram_app.initialize())


async def message_exists(message_id: int) -> bool:
    """
    Telegram TIDAK mengirim update saat pesan channel dihapus, jadi keberadaan
    pesan dicek dengan menyalin pesan ke chat admin lalu langsung menghapus salinannya.
    """
    bot = telegram_app.bot
    while True:
        try:
            copied = await bot.copy_message(
                chat_id=ADMIN_CHAT_ID, from_chat_id=CHANNEL_ID, message_id=message_id,
                disable_notification=True,
            )
            try:
                await bot.delete_message(chat_id=ADMIN_CHAT_ID, message_id=copied.message_id)
            except Exception:
                pass
            return True
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except BadRequest as e:
            msg = str(e).lower()
            if "not found" in msg or "message_id_invalid" in msg:
                return False
            raise  # error lain: jangan anggap terhapus


async def sync_deleted():
    sheet_ids = await asyncio.to_thread(lambda: get_google_sheet().col_values(1))
    ids = [int(v) for v in sheet_ids if v.isdigit()]
    gone = set()
    for mid in ids:
        try:
            if not await message_exists(mid):
                gone.add(str(mid))
        except Exception:
            log.exception("Gagal memeriksa pesan %s, dilewati", mid)
        await asyncio.sleep(0.2)  # hormati rate limit Telegram
    removed = await asyncio.to_thread(delete_rows_by_message_ids, gone) if gone else 0
    return len(ids), removed


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def index():
    return "OK", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(header, WEBHOOK_SECRET):
        abort(403)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400)
    try:
        update = Update.de_json(data, telegram_app.bot)
        run_async(telegram_app.process_update(update), timeout=60)
    except Exception:
        log.exception("Gagal memproses update")
    return "OK", 200


@app.route("/sync", methods=["POST"])
def sync():
    """Dipanggil cron (mis. cron-job.org) dengan header X-Sync-Secret."""
    if not SYNC_SECRET or not ADMIN_CHAT_ID:
        abort(503)
    if not hmac.compare_digest(request.headers.get("X-Sync-Secret", ""), SYNC_SECRET):
        abort(403)
    checked, removed = run_async(sync_deleted(), timeout=3000)
    return {"checked": checked, "removed": removed}, 200


if __name__ == "__main__":
    # Produksi: gunicorn -w 1 --threads 4 -t 3000 app:app
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
