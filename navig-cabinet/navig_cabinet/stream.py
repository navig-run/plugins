"""Chunked AES-256-GCM — encrypt a file of any size without holding it in memory.

A cabinet holds scans and PDFs, but also voice notes and multi-gigabyte videos, so an
item is never sealed as one blob. It is cut into ``chunk_size`` pieces and each piece
is sealed on its own (the STREAM construction):

    header  = MAGIC ‖ u32 chunk_size
    record  = u32 len(ct) ‖ nonce(12) ‖ ct            (ct includes the 16-byte GCM tag)
    aad_i   = header ‖ context ‖ u64 index ‖ u8 is_last

Binding the index into every chunk's AAD makes reordering fail; binding ``is_last``
makes truncation fail (a stream that ends without a final chunk is rejected, and so
is anything after it); binding ``context`` (the item id) makes splicing one item's
chunks into another fail. Peak memory is about two chunks.

The format is deliberately simple enough to reimplement from this docstring alone —
``recover_backup.py`` does exactly that with nothing but ``cryptography``.
"""

from __future__ import annotations

import hashlib
import io
import os
import struct
from typing import BinaryIO

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"NCABSTR1"
DEFAULT_CHUNK = 4 * 1024 * 1024
_NONCE = 12
_TAG = 16
_LEN = struct.Struct(">I")
_AAD_TAIL = struct.Struct(">QB")


class StreamError(Exception):
    """The stream is corrupt, truncated, tampered with, or the key is wrong."""


def _header(chunk_size: int) -> bytes:
    return MAGIC + _LEN.pack(chunk_size)


class StreamEncryptor:
    """A write-only file object that encrypts everything written to it.

    It holds back the most recent full chunk until more data arrives, because a chunk
    can only be sealed once we know whether it is the last one. ``close()`` seals the
    final chunk — which may be empty, and must still be written, or the reader would
    rightly call the stream truncated.
    """

    def __init__(self, dst: BinaryIO, key: bytes, context: bytes, chunk_size: int = DEFAULT_CHUNK):
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        self._dst = dst
        self._aead = AESGCM(key)
        self._context = context
        self._chunk = chunk_size
        self._header = _header(chunk_size)
        self._buf = bytearray()
        self._index = 0
        self._closed = False
        self.size = 0
        self._sha = hashlib.sha256()
        dst.write(self._header)

    # ``tarfile`` streaming mode and ``shutil.copyfileobj`` only need write().
    def write(self, data: bytes) -> int:
        if self._closed:
            raise ValueError("write to a closed StreamEncryptor")
        self._buf += data
        self.size += len(data)
        self._sha.update(data)
        while len(self._buf) > self._chunk:
            self._emit(bytes(self._buf[: self._chunk]), last=False)
            del self._buf[: self._chunk]
        return len(data)

    def _emit(self, plain: bytes, *, last: bool) -> None:
        aad = self._header + self._context + _AAD_TAIL.pack(self._index, 1 if last else 0)
        nonce = os.urandom(_NONCE)
        ct = self._aead.encrypt(nonce, plain, aad)
        self._dst.write(_LEN.pack(len(ct)) + nonce + ct)
        self._index += 1

    def close(self) -> None:
        if self._closed:
            return
        self._emit(bytes(self._buf), last=True)
        self._buf.clear()
        self._closed = True

    @property
    def sha256(self) -> str:
        return self._sha.hexdigest()

    def __enter__(self) -> StreamEncryptor:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.close()


class StreamDecryptor(io.RawIOBase):
    """A read-only file object over an encrypted stream.

    Every chunk is authenticated before a single byte of it is returned. Reaching the
    end of the underlying file without having seen the final chunk raises, as does
    finding anything after it — so a reader that consumes to EOF has proved the stream
    is complete and untouched.
    """

    def __init__(self, src: BinaryIO, key: bytes, context: bytes):
        super().__init__()
        self._src = src
        self._aead = AESGCM(key)
        self._context = context
        head = _read_exact(src, len(MAGIC) + _LEN.size)
        if len(head) < len(MAGIC) + _LEN.size or head[: len(MAGIC)] != MAGIC:
            raise StreamError("not a cabinet stream (bad header)")
        (self._chunk,) = _LEN.unpack(head[len(MAGIC):])
        self._header = head
        self._index = 0
        self._done = False
        self._buf = b""
        self._pos = 0
        self.size = 0
        self._sha = hashlib.sha256()

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        try:
            self._src.close()
        finally:
            super().close()

    def _next_chunk(self) -> None:
        raw_len = _read_exact(self._src, _LEN.size)
        if len(raw_len) < _LEN.size:
            raise StreamError("stream is truncated (no final chunk)")
        (ct_len,) = _LEN.unpack(raw_len)
        if ct_len < _TAG or ct_len > self._chunk + _TAG:
            raise StreamError("stream is corrupt (impossible chunk length)")
        nonce = _read_exact(self._src, _NONCE)
        ct = _read_exact(self._src, ct_len)
        if len(nonce) < _NONCE or len(ct) < ct_len:
            raise StreamError("stream is truncated (partial chunk)")
        plain = None
        for last in (0, 1):
            aad = self._header + self._context + _AAD_TAIL.pack(self._index, last)
            try:
                plain = self._aead.decrypt(nonce, ct, aad)
            except Exception:  # noqa: BLE001 — InvalidTag; try the other flag
                continue
            if last:
                self._done = True
                if self._src.read(1):
                    raise StreamError("stream is corrupt (data after the final chunk)")
            break
        if plain is None:
            raise StreamError("decryption failed — wrong key, or the data was tampered with")
        self._index += 1
        self._buf = plain
        self._pos = 0
        self.size += len(plain)
        self._sha.update(plain)

    def readinto(self, b) -> int:  # type: ignore[override]
        while self._pos >= len(self._buf):
            if self._done:
                return 0
            self._next_chunk()
        n = min(len(b), len(self._buf) - self._pos)
        b[:n] = self._buf[self._pos: self._pos + n]
        self._pos += n
        return n

    def read_to_end(self, sink: BinaryIO | None = None, bufsize: int = 1024 * 1024) -> int:
        """Consume (and verify) the rest of the stream, optionally copying it out."""
        total = 0
        while True:
            chunk = self.read(bufsize)
            if not chunk:
                return total
            total += len(chunk)
            if sink is not None:
                sink.write(chunk)

    @property
    def complete(self) -> bool:
        return self._done and self._pos >= len(self._buf)

    @property
    def sha256(self) -> str:
        return self._sha.hexdigest()


def _read_exact(src: BinaryIO, n: int) -> bytes:
    parts = []
    need = n
    while need:
        got = src.read(need)
        if not got:
            break
        parts.append(got)
        need -= len(got)
    return b"".join(parts)


def encrypt_file(src: BinaryIO, dst: BinaryIO, key: bytes, context: bytes,
                 chunk_size: int = DEFAULT_CHUNK) -> tuple[int, str]:
    """Encrypt *src* into *dst*. Returns ``(plaintext_size, plaintext_sha256)``."""
    enc = StreamEncryptor(dst, key, context, chunk_size)
    while True:
        block = src.read(chunk_size)
        if not block:
            break
        enc.write(block)
    enc.close()
    return enc.size, enc.sha256


def decrypt_file(src: BinaryIO, dst: BinaryIO | None, key: bytes, context: bytes) -> tuple[int, str]:
    """Decrypt *src* into *dst* (or just verify, when *dst* is None)."""
    dec = StreamDecryptor(src, key, context)
    dec.read_to_end(dst)
    return dec.size, dec.sha256
