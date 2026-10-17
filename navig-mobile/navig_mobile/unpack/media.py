"""Engine-agnostic media helpers: batched game-audio → OGG, Ogg carving, DDS → PNG."""

from __future__ import annotations

import struct
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from navig_mobile.unpack import tools


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _audio_batch(src_dir: Path, names: list[str], out_dir: Path, subsongs: bool) -> int:
    """Decode a batch with one vgmstream call, encode with one ffmpeg call per 50 files.

    vgmstream runs inside the source folder so its ``?f`` wildcard is a bare file name; each batch
    decodes into its own temp folder so parallel batches never pick up each other's WAVs.
    """
    vgm, ffmpeg = tools.require("vgmstream"), tools.require("ffmpeg")
    tmp = out_dir / f".tmp_{abs(hash((str(src_dir), names[0])))}"
    tmp.mkdir(parents=True, exist_ok=True)
    pattern = str(tmp / ("?f_?04s.wav" if subsongs else "?f.wav"))
    subprocess.run([vgm] + (["-S", "0"] if subsongs else []) + ["-o", pattern] + names,
                   cwd=src_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for group in _chunks(sorted(tmp.glob("*.wav")), 50):
        cmd = [ffmpeg, "-nostdin", "-loglevel", "error", "-y"]
        for w in group:
            cmd += ["-i", str(w)]
        for i, w in enumerate(group):
            stem = w.name
            for ext in (".wem.wav", ".bnk", ".xwb", ".wav"):
                stem = stem.replace(ext, "")
            cmd += ["-map", f"{i}:a", "-c:a", "libvorbis", "-q:a", "4", str(out_dir / f"{stem}.ogg")]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for w in tmp.glob("*"):
        w.unlink()
    tmp.rmdir()
    if out_dir.exists() and not any(out_dir.iterdir()):
        out_dir.rmdir()
    return len(names)


def convert_audio(src: Path, dst: Path, pattern: str = "*.wem", subsongs: bool = False, jobs: int = 16) -> dict:
    """Convert every file matching ``pattern`` under ``src`` to .ogg under ``dst`` (layout preserved).

    ``subsongs`` = containers holding many sounds (.bnk, .xwb, .fsb): one folder per container.
    Existing outputs are skipped, so an interrupted run resumes.
    """
    src, dst = src.resolve(), dst.resolve()
    by_dir: dict[tuple[Path, Path], list[str]] = {}
    for f in src.rglob(pattern):
        out_dir = dst / f.parent.relative_to(src)
        if subsongs:
            out_dir = out_dir / f.stem
        elif (out_dir / (f.stem + ".ogg")).exists():
            continue
        by_dir.setdefault((f.parent, out_dir), []).append(f.name)
    tasks = [(d, b, o) for (d, o), names in by_dir.items() for b in _chunks(sorted(names), 1 if subsongs else 120)]
    done = 0
    with ThreadPoolExecutor(jobs) as pool:
        for n in pool.map(lambda t: _audio_batch(t[0], t[1], t[2], subsongs), tasks):
            done += n
    return {"inputs": done, "ogg": sum(1 for _ in dst.rglob("*.ogg")) if dst.exists() else 0,
            "leftover_wav": sum(1 for _ in dst.rglob("*.wav")) if dst.exists() else 0}


# ── Ogg carving ─────────────────────────────────────────────────────────────

_CRC = []
for _i in range(256):
    _r = _i << 24
    for _ in range(8):
        _r = ((_r << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if _r & 0x80000000 else (_r << 1) & 0xFFFFFFFF
    _CRC.append(_r)


def _ogg_crc(data: bytes) -> int:
    c = 0
    for b in data:
        c = ((c << 8) & 0xFFFFFFFF) ^ _CRC[(c >> 24) ^ b]
    return c


def _ogg_pages(d: bytes):
    i = d.find(b"OggS")
    while i != -1:
        if i + 27 <= len(d) and d[i + 4] == 0:
            nseg = d[i + 26]
            size = 27 + nseg + sum(d[i + 27:i + 27 + nseg])
            page = d[i:i + size]
            if len(page) == size and _ogg_crc(page[:22] + b"\0\0\0\0" + page[26:]) == struct.unpack_from("<I", page, 22)[0]:
                yield i, page
                i = d.find(b"OggS", i + size)
                continue
        i = d.find(b"OggS", i + 1)


def carve_ogg(container: Path, out: Path) -> int:
    """Write every complete, CRC-valid Ogg stream (BOS..EOS) found inside ``container``."""
    d = container.read_bytes()
    out.mkdir(parents=True, exist_ok=True)
    streams, n = {}, 0
    for off, page in _ogg_pages(d):
        flags, serial = page[5], struct.unpack_from("<I", page, 14)[0]
        if flags & 0x02:
            streams[serial] = [off, [page]]
        elif serial in streams:
            streams[serial][1].append(page)
        if flags & 0x04 and serial in streams:
            start, pages = streams.pop(serial)
            ext = ".opus" if b"OpusHead" in pages[0] else ".ogg"
            (out / f"sound_{start:010d}{ext}").write_bytes(b"".join(pages))
            n += 1
    return n


# ── images ──────────────────────────────────────────────────────────────────

def dds_to_png(src: Path, dst: Path) -> tuple[int, int]:
    """Convert every .dds under ``src`` to .png under ``dst``; undecodable ones are copied as .dds."""
    from PIL import Image

    ok = bad = 0
    for f in [p for p in src.rglob("*") if p.suffix.lower() == ".dds"]:
        out = dst / f.relative_to(src).with_suffix(".png")
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            Image.open(f).save(out)
            ok += 1
        except Exception:
            out.with_suffix(".dds").write_bytes(f.read_bytes())
            bad += 1
    return ok, bad
