"""Logika murni (tanpa dependensi luar): tanda tangan link dan parsing header Range."""
import hashlib
import hmac
import time

MAX_TTL = 6 * 3600  # link tidak boleh berlaku lebih dari 6 jam ke depan


def make_signature(secret: str, message_id: int, exp: int) -> str:
    msg = f"{message_id}.{exp}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def verify(secret: str, message_id: int, exp, sig, now=None) -> bool:
    now = int(time.time()) if now is None else now
    try:
        exp = int(exp)
    except (TypeError, ValueError):
        return False
    if exp < now or exp - now > MAX_TTL:
        return False
    expected = make_signature(secret, message_id, exp).encode()
    return hmac.compare_digest(expected, str(sig or "").encode())


def parse_range(header, size: int):
    """
    Return None  -> kirim seluruh file (tanpa Range / format tidak didukung)
    Return (start, end) inklusif -> kirim sebagian (206)
    Raise ValueError -> rentang tidak bisa dipenuhi (416)
    """
    if not header:
        return None
    header = header.strip()
    if not header.lower().startswith("bytes="):
        return None
    spec = header[6:].strip()
    if "," in spec:  # multi-range tidak didukung -> kirim penuh
        return None
    if "-" not in spec:
        raise ValueError("format range salah")
    a, b = (x.strip() for x in spec.split("-", 1))
    if a == "":  # suffix: "-500" = 500 byte terakhir
        if not b.isdigit() or int(b) == 0:
            raise ValueError("suffix salah")
        start, end = max(size - int(b), 0), size - 1
    else:
        if not a.isdigit() or (b and not b.isdigit()):
            raise ValueError("angka salah")
        start = int(a)
        end = min(int(b), size - 1) if b else size - 1
    if size <= 0 or start >= size or start > end:
        raise ValueError("di luar jangkauan")
    return start, end
