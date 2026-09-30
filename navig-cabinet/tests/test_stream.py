"""The chunked stream: round trips at every boundary, and every tampering shape fails."""

from __future__ import annotations

import io
import os

import pytest
from navig_cabinet.stream import MAGIC, StreamError, decrypt_file, encrypt_file

KEY = bytes(range(32))
CTX = b"item-1"


def _enc(data: bytes, chunk: int = 16, ctx: bytes = CTX) -> bytes:
    out = io.BytesIO()
    encrypt_file(io.BytesIO(data), out, KEY, ctx, chunk_size=chunk)
    return out.getvalue()


def _dec(blob: bytes, ctx: bytes = CTX, key: bytes = KEY) -> bytes:
    out = io.BytesIO()
    decrypt_file(io.BytesIO(blob), out, key, ctx)
    return out.getvalue()


@pytest.mark.parametrize("size", [0, 1, 15, 16, 17, 31, 32, 33, 100])
def test_round_trip_at_every_chunk_boundary(size):
    data = os.urandom(size)
    assert _dec(_enc(data)) == data


def test_ciphertext_does_not_contain_the_plaintext():
    data = b"PASSPORT NUMBER 12AB34567 " * 10
    assert b"PASSPORT" not in _enc(data)


def _records(blob: bytes) -> tuple[bytes, list[bytes]]:
    head, rest, recs = blob[:12], blob[12:], []
    while rest:
        n = int.from_bytes(rest[:4], "big")
        recs.append(rest[: 4 + 12 + n])
        rest = rest[4 + 12 + n:]
    return head, recs


def test_reordered_chunks_fail():
    head, recs = _records(_enc(os.urandom(64)))
    assert len(recs) >= 3
    recs[0], recs[1] = recs[1], recs[0]
    with pytest.raises(StreamError):
        _dec(head + b"".join(recs))


def test_truncated_stream_fails_even_on_a_chunk_boundary():
    """Dropping the final chunk leaves a well-formed prefix — it must still be rejected."""
    head, recs = _records(_enc(os.urandom(64)))
    with pytest.raises(StreamError, match="truncated"):
        _dec(head + b"".join(recs[:-1]))


def test_trailing_garbage_fails():
    with pytest.raises(StreamError):
        _dec(_enc(b"hello") + b"x")


def test_flipped_bit_fails():
    blob = bytearray(_enc(os.urandom(40)))
    blob[30] ^= 1
    with pytest.raises(StreamError):
        _dec(bytes(blob))


def test_chunks_cannot_be_spliced_between_items():
    with pytest.raises(StreamError):
        _dec(_enc(b"item one contents", ctx=b"one"), ctx=b"two")


def test_wrong_key_fails():
    with pytest.raises(StreamError):
        _dec(_enc(b"x" * 40), key=bytes(32))


def test_not_a_stream():
    with pytest.raises(StreamError, match="header"):
        _dec(b"nope" * 10)
    assert MAGIC == b"NCABSTR1"
