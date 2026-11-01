import os
import json
import hmac
import time
import logging
import threading
from datetime import datetime

import requests
from flask import Flask, request, abort
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

TG_API = f"https://api.telegram.org/bot{TOKEN}"

# ---------------------------------------------------------------------------
# Google Sheets (client di-cache; akses diserialkan dengan lock)
# ---------------------------------------------------------------------------
_sheet_lock = threading.Lock()
_sheet = None


def get_google_sheet():
    global _sheet
    if _sheet is None:
        log.info("Menghubungkan ke Google Sheets...")
        if GOOGLE_CREDENTIALS_JSON:
            client = gspread.service_account_from_dict(json.loads(GOOGLE_CREDENTIALS_JSON))
        else:
            client = gspread.service_account(filename="credentials.json")
        client.set_timeout(30)  # jangan menggantung selamanya jika Google lambat
        _sheet = client.open(SHEET_NAME).sheet1
        log.info("Terhubung ke spreadsheet '%s'", SHEET_NAME)
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
# Memproses update dari Telegram (tanpa library bot, langsung dari JSON)
# ---------------------------------------------------------------------------
def extract_video(update: dict):
    """Kembalikan (message_id, file_id, caption) jika update adalah video dari channel target."""
    post = update.get("channel_post")
    if not isinstance(post, dict):
        return None
    chat = post.get("chat") or {}
    if chat.get("id") != CHANNEL_ID:
        log.warning("Channel post diabaikan: chat.id=%s, CHANNEL_ID=%s", chat.get("id"), CHANNEL_ID)
        return None
    media = post.get("video")
    if not media:
        doc = post.get("document") or {}
        if str(doc.get("mime_type", "")).startswith("video/"):
            media = doc
    if not media or "file_id" not in media or "message_id" not in post:
        log.info("Channel post bukan video, dilewati (message_id=%s)", post.get("message_id"))
        return None
    return post["message_id"], media["file_id"], post.get("caption") or "Tanpa Keterangan"


def process_update(update: dict):
    try:
        found = extract_video(update)
        if not found:
            return
        message_id, file_id, caption = found
        log.info("Video diterima dari channel. Message ID: %s", message_id)
        added = record_video(message_id, file_id, caption)
        log.info("Video %s. Message ID: %s", "dicatat" if added else "sudah ada (dilewati)", message_id)
    except Exception:
        log.exception("Gagal mencatat ke Google Sheets")


# ---------------------------------------------------------------------------
# Sinkron hapus: Telegram TIDAK mengirim update saat pesan channel dihapus,
# jadi keberadaan pesan dicek dengan menyalin pesan ke chat admin lalu
# langsung menghapus salinannya.
# ---------------------------------------------------------------------------
def tg(method: str, **params):
    r = requests.post(f"{TG_API}/{method}", json=params, timeout=20)
    try:
        data = r.json()
    except ValueError:
        data = {}
    return r.status_code, data


def message_exists(message_id: int) -> bool:
    while True:
        code, data = tg(
            "copyMessage", chat_id=ADMIN_CHAT_ID, from_chat_id=CHANNEL_ID,
            message_id=message_id, disable_notification=True,
        )
        if data.get("ok"):
            try:
                tg("deleteMessage", chat_id=ADMIN_CHAT_ID, message_id=data["result"]["message_id"])
            except Exception:
                pass
            return True
        desc = str(data.get("description", "")).lower()
        if code == 429:
            time.sleep(int((data.get("parameters") or {}).get("retry_after", 5)) + 1)
            continue
        # Hanya jawaban spesifik ini yang berarti pesan sudah dihapus. Error lain
        # (mis. "chat not found" karena ADMIN_CHAT_ID salah) JANGAN dianggap terhapus.
        if code == 400 and ("message to copy not found" in desc or "message_id_invalid" in desc):
            return False
        raise RuntimeError(f"Telegram: {code} {desc}")  # error lain: jangan anggap terhapus


def sync_deleted():
    with _sheet_lock:
        col = get_google_sheet().col_values(1)
    ids = [int(v) for v in col if v.isdigit()]
    gone = set()
    for mid in ids:
        try:
            if not message_exists(mid):
                gone.add(str(mid))
        except Exception:
            log.exception("Gagal memeriksa pesan %s, dilewati", mid)
        time.sleep(0.2)  # hormati rate limit Telegram
    # Pengaman: jika SEMUA pesan tampak hilang, kemungkinan besar ada salah konfigurasi
    # (bukan benar-benar terhapus semua), jadi jangan hapus apa pun.
    if len(ids) >= 3 and len(gone) == len(ids):
        log.error("Semua %s pesan tampak hilang; penghapusan dibatalkan. Periksa CHANNEL_ID/ADMIN_CHAT_ID.", len(ids))
        return len(ids), 0
    removed = delete_rows_by_message_ids(gone) if gone else 0
    return len(ids), removed


_sync_running = threading.Lock()


def sync_job():
    try:
        log.info("Sinkron hapus dimulai")
        checked, removed = sync_deleted()
        log.info("Sinkron selesai: diperiksa=%s, dihapus=%s", checked, removed)
    except Exception:
        log.exception("Sinkron gagal")
    finally:
        _sync_running.release()


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
    kind = next((k for k in data if k != "update_id"), "?")
    chat = (data.get(kind) or {}).get("chat") or {} if isinstance(data.get(kind), dict) else {}
    log.info(
        "Update masuk: jenis=%s, tipe_chat=%s, chat.id=%s (CHANNEL_ID=%s)",
        kind, chat.get("type"), chat.get("id"), CHANNEL_ID,
    )
    # Proses di thread terpisah agar Telegram langsung mendapat jawaban 200.
    threading.Thread(target=process_update, args=(data,), daemon=True).start()
    return "OK", 200


@app.route("/sync", methods=["GET", "POST"])  # GET juga diterima karena cron sering default ke GET; tetap wajib secret
def sync():
    """Dipanggil cron (mis. cron-job.org) dengan header X-Sync-Secret."""
    if not SYNC_SECRET or not ADMIN_CHAT_ID:
        abort(503)
    if not hmac.compare_digest(request.headers.get("X-Sync-Secret", ""), SYNC_SECRET):
        abort(403)
    if not _sync_running.acquire(blocking=False):
        return {"status": "sudah berjalan"}, 202
    threading.Thread(target=sync_job, daemon=True).start()
    return {"status": "dimulai, hasil ada di Logs"}, 202


if __name__ == "__main__":
    # Produksi: gunicorn -w 1 --threads 4 app:app
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
