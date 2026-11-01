import subprocess
import sys
import pytest
from streamcore import make_signature, verify, parse_range, MAX_TTL

S = "rahasia-rahasia-123"
NOW = 1_800_000_000


# ---------- tanda tangan ----------
def test_valid_signature():
    sig = make_signature(S, 22, NOW + 600)
    assert verify(S, 22, NOW + 600, sig, now=NOW)


def test_wrong_secret_rejected():
    sig = make_signature("rahasia-lain-12345678", 22, NOW + 600)
    assert not verify(S, 22, NOW + 600, sig, now=NOW)


def test_tampered_message_id_rejected():
    sig = make_signature(S, 22, NOW + 600)
    assert not verify(S, 23, NOW + 600, sig, now=NOW)


def test_tampered_exp_rejected():
    sig = make_signature(S, 22, NOW + 600)
    assert not verify(S, 22, NOW + 900, sig, now=NOW)


def test_expired_rejected():
    sig = make_signature(S, 22, NOW - 1)
    assert not verify(S, 22, NOW - 1, sig, now=NOW)


def test_too_long_ttl_rejected():
    exp = NOW + MAX_TTL + 1
    assert not verify(S, 22, exp, make_signature(S, 22, exp), now=NOW)


@pytest.mark.parametrize("exp,sig", [(None, "x"), ("abc", "x"), (NOW + 60, None), (NOW + 60, ""), (NOW + 60, "é\u0000")])
def test_garbage_rejected(exp, sig):
    assert not verify(S, 22, exp, sig, now=NOW)


# ---------- Range ----------
def test_no_range_is_full():
    assert parse_range(None, 1000) is None
    assert parse_range("", 1000) is None
    assert parse_range("items=0-5", 1000) is None


def test_open_ended_and_closed():
    assert parse_range("bytes=0-", 1000) == (0, 999)
    assert parse_range("bytes=100-199", 1000) == (100, 199)
    assert parse_range("bytes=900-5000", 1000) == (900, 999)  # end dipangkas


def test_suffix():
    assert parse_range("bytes=-200", 1000) == (800, 999)
    assert parse_range("bytes=-5000", 1000) == (0, 999)


def test_multi_range_falls_back_to_full():
    assert parse_range("bytes=0-1,5-9", 1000) is None


@pytest.mark.parametrize("h", ["bytes=1000-", "bytes=2000-3000", "bytes=500-100", "bytes=-0", "bytes=abc-", "bytes=5"])
def test_unsatisfiable(h):
    with pytest.raises(ValueError):
        parse_range(h, 1000)


def test_empty_file():
    with pytest.raises(ValueError):
        parse_range("bytes=0-", 0)


# ---------- make_link.py harus menghasilkan tanda tangan yang diterima verify ----------
def test_make_link_output_verifies():
    import os, re, time
    env = dict(os.environ, STREAM_SECRET=S)
    out = subprocess.check_output(
        [sys.executable, "make_link.py", "https://x.example", "22", "30"], env=env, text=True
    ).strip()
    m = re.fullmatch(r"https://x\.example/v/22\?exp=(\d+)&sig=([0-9a-f]{64})", out)
    assert m, out
    assert verify(S, 22, m.group(1), m.group(2))
