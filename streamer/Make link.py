"""
Membuat link tonton bertanda tangan untuk tes.
Pakai: python make_link.py https://nama-streamer.onrender.com 22 [menit]
  22     = Message ID video (kolom A di spreadsheet)
  menit  = masa berlaku link (default 60, maksimal 360)
Rahasia dibaca dari variabel STREAM_SECRET, atau ditanyakan jika kosong.
"""
import os
import sys
import time
import hmac
import hashlib
import getpass


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    base, message_id = sys.argv[1].rstrip("/"), int(sys.argv[2])
    minutes = min(int(sys.argv[3]) if len(sys.argv) > 3 else 60, 360)
    secret = os.environ.get("STREAM_SECRET") or getpass.getpass("STREAM_SECRET: ")
    exp = int(time.time()) + minutes * 60
    sig = hmac.new(secret.encode(), f"{message_id}.{exp}".encode(), hashlib.sha256).hexdigest()
    print(f"{base}/v/{message_id}?exp={exp}&sig={sig}")


if __name__ == "__main__":
    main()
