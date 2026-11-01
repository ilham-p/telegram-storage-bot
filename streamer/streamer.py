"""
Layanan streaming video dari channel Telegram lewat MTProto (Telethon).
Bot memakai jalur MTProto sehingga TIDAK terkena batas unduh 20 MB milik Bot HTTP API.

Jalankan: uvicorn streamer:app --host 0.0.0.0 --port $PORT

Environment variables:
  BOT_TOKEN      token bot (bot harus admin/anggota channel)
  CHANNEL_ID     contoh -1003971007907
  API_ID         dari https://my.telegram.org  (API development tools)
  API_HASH       dari https://my.telegram.org
  STREAM_SECRET  rahasia acak (min. 16 karakter), sama dengan yang dipakai pembuat link
"""
import os
import time
import asyncio
import logging
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.responses import Response, StreamingResponse, PlainTextResponse
from starlette.routing import Route
from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetChannelsRequest
from telethon.tl.types import InputChannel

from streamcore import verify, parse_range

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("streamer")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0"))
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH")
STREAM_SECRET = os.environ.get("STREAM_SECRET", "")

if not (BOT_TOKEN and CHANNEL_ID and API_ID and API_HASH):
    raise RuntimeError("BOT_TOKEN, CHANNEL_ID, API_ID, dan API_HASH wajib diisi.")
if len(STREAM_SECRET) < 16:
    raise RuntimeError("STREAM_SECRET wajib diisi (min. 16 karakter).")

CHUNK = 512 * 1024  # kelipatan 4096 dan pembagi 1 MB (syarat Telegram)
DOC_CACHE_TTL = 120

client = None          # dibuat di dalam event loop (lihat lifespan)
_channel = None
_doc_cache = {}        # message_id -> (waktu, document)


async def start_client():
    global client
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    for _ in range(3):
        try:
            await client.start(bot_token=BOT_TOKEN)
            return
        except FloodWaitError as e:
            log.warning("FloodWait %s detik saat login, menunggu...", e.seconds)
            await asyncio.sleep(e.seconds + 1)
    raise RuntimeError("Gagal login ke Telegram")


@asynccontextmanager
async def lifespan(app):
    await start_client()
    log.info("Terhubung ke Telegram (MTProto). Siap streaming.")
    try:
        yield
    finally:
        await client.disconnect()


async def get_channel():
    """Bot yang baru login belum mengenal channel; ambil lewat GetChannels (bot harus anggota)."""
    global _channel
    if _channel is None:
        real_id, _ = utils.resolve_id(CHANNEL_ID)
        try:
            _channel = await client.get_input_entity(CHANNEL_ID)
        except ValueError:
            res = await client(GetChannelsRequest([InputChannel(real_id, 0)]))
            _channel = utils.get_input_peer(res.chats[0])
        log.info("Channel berhasil di-resolve")
    return _channel


async def get_document(message_id: int):
    now = time.time()
    hit = _doc_cache.get(message_id)
    if hit and now - hit[0] < DOC_CACHE_TTL:
        return hit[1]
    msg = await client.get_messages(await get_channel(), ids=message_id)
    doc = getattr(msg, "document", None) if msg else None
    mime = getattr(doc, "mime_type", "") or ""
    doc = doc if doc and mime.startswith("video/") else None
    if doc:
        _doc_cache[message_id] = (now, doc)
    return doc


async def body(doc, start: int, end: int):
    aligned = start - (start % CHUNK)
    skip = start - aligned
    remaining = end - start + 1
    it = client.iter_download(doc, offset=aligned, request_size=CHUNK, file_size=doc.size)
    try:
        async for chunk in it:
            if skip:
                chunk = chunk[skip:]
                skip = 0
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
            remaining -= len(chunk)
            if chunk:
                yield bytes(chunk)
            if remaining <= 0:
                break
    finally:
        close = getattr(it, "close", None)
        if close:
            try:
                await close()
            except Exception:
                pass


async def stream(request):
    message_id = request.path_params["message_id"]
    q = request.query_params
    if not verify(STREAM_SECRET, message_id, q.get("exp"), q.get("sig")):
        return PlainTextResponse("Link tidak valid atau sudah kedaluwarsa", status_code=403)
    try:
        doc = await get_document(message_id)
    except Exception:
        log.exception("Gagal mengambil pesan %s dari Telegram", message_id)
        return PlainTextResponse("Gagal menghubungi Telegram", status_code=502)
    if not doc:
        return PlainTextResponse("Video tidak ditemukan", status_code=404)

    size, mime = doc.size, doc.mime_type
    try:
        rng = parse_range(request.headers.get("range"), size)
    except ValueError:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})

    if rng is None:
        start, end, status = 0, size - 1, 200
    else:
        (start, end), status = rng, 206
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Cache-Control": "private, no-store",
        "Content-Disposition": "inline",
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    log.info("Stream pesan=%s range=%s-%s dari %s", message_id, start, end, size)

    if request.method == "HEAD":
        return Response(status_code=status, headers=headers, media_type=mime)
    return StreamingResponse(body(doc, start, end), status_code=status, headers=headers, media_type=mime)


async def health(request):
    return PlainTextResponse("OK" if client and client.is_connected() else "starting")


app = Starlette(
    routes=[
        Route("/", health),
        Route("/v/{message_id:int}", stream, methods=["GET", "HEAD"]),
    ],
    lifespan=lifespan,
)
