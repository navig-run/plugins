"""Multi-language dubbing: the same voices, translated, with the timing kept.

ElevenLabs Dubbing does the hard part (transcribe → translate → re-speak in a clone of
each speaker's voice, background kept). NAVIG keeps everything around it honest:

* only the sound is uploaded — for a video the picture never leaves the machine and is
  copied back bit for bit, with the dub as the first audio track and the original kept
  as a second, so a player can switch;
* consent is per run (``allow_cloud``) and the result is labelled AI-generated in a
  ``.dub.json`` sidecar;
* timing is checked: a dub whose length drifts more than 2 % from the source is
  reported, not silently shipped.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

POLL_S = 5.0
TIMEOUT_S = 1800.0
DRIFT_TOLERANCE = 0.02
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".m4v", ".webm"}
# Container language tags are ISO 639-2 (three letters); MP4 drops a two-letter one.
ISO639_2 = {"en": "eng", "fr": "fra", "es": "spa", "de": "deu", "it": "ita", "pt": "por", "ru": "rus", "ja": "jpn",
            "ko": "kor", "zh": "zho", "ar": "ara", "hi": "hin", "nl": "nld", "pl": "pol", "tr": "tur", "sv": "swe",
            "uk": "ukr", "cs": "ces", "el": "ell", "he": "heb", "id": "ind", "vi": "vie", "th": "tha", "ro": "ron"}


class DubError(RuntimeError):
    """No consent, provider failure, timeout, or ffmpeg failure."""


class DubProvider(Protocol):
    async def start(self, src: Path, target_lang: str, source_lang: str | None, num_speakers: int) -> str: ...
    async def status(self, dubbing_id: str) -> dict[str, Any]: ...
    async def download(self, dubbing_id: str, lang: str) -> bytes: ...


class ElevenLabsDubbing:
    async def _with(self, fn):
        from navig_generate.tools.audio_generation import AudioGenerationConfig, AudioGenerator

        gen = AudioGenerator(AudioGenerationConfig(save_locally=False))
        try:
            return await fn(gen)
        finally:
            await gen.close()

    async def start(self, src: Path, target_lang: str, source_lang: str | None, num_speakers: int) -> str:
        return await self._with(lambda g: g.dub_start(src, target_lang, source_lang=source_lang, num_speakers=num_speakers))

    async def status(self, dubbing_id: str) -> dict[str, Any]:
        return await self._with(lambda g: g.dub_status(dubbing_id))

    async def download(self, dubbing_id: str, lang: str) -> bytes:
        return await self._with(lambda g: g.dub_download(dubbing_id, lang))


@dataclass
class DubTake:
    language: str
    output: str
    duration_s: float
    drift: float  # relative length difference to the source

    @property
    def in_sync(self) -> bool:
        return abs(self.drift) <= DRIFT_TOLERANCE


@dataclass
class DubResult:
    source: str
    takes: list[DubTake] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    sidecar: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "generated": True, "sidecar": self.sidecar, "failed": self.failed,
                "takes": [{**t.__dict__, "in_sync": t.in_sync} for t in self.takes]}


def _ffmpeg(*args: str, timeout: int = 1800) -> None:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise DubError("ffmpeg is not on PATH")
    proc = subprocess.run([exe, "-hide_banner", "-nostdin", "-y", "-v", "error", *args],  # noqa: S603
                          capture_output=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise DubError(f"ffmpeg failed: {tail[-1] if tail else 'no output'}")


def _duration(path: Path) -> float:
    from navig_audio.enhance.analysis import probe

    return float(probe(path)["duration_s"])


def _has_audio(path: Path) -> bool:
    exe = shutil.which("ffprobe")
    if not exe:
        raise DubError("ffprobe is not on PATH")
    out = subprocess.run([exe, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0",  # noqa: S603
                          str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False)
    return bool(out.stdout.strip())


async def _wait(provider: DubProvider, dubbing_id: str, poll_s: float, timeout_s: float,
                progress: Callable[[str], None] | None) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        st = await provider.status(dubbing_id)
        state = str(st.get("status", "")).lower()
        if state == "dubbed":
            return
        if st.get("error") or state in ("failed", "error"):
            raise DubError(f"the dub failed: {st.get('error') or state}")
        if time.monotonic() > deadline:
            raise DubError(f"the dub was not ready after {timeout_s / 60:.0f} min (id {dubbing_id})")
        if progress:
            progress(state or "working")
        await asyncio.sleep(poll_s)


def dub(src: Path | str, languages: list[str], *, allow_cloud: bool = False, source_lang: str | None = None,
        num_speakers: int = 0, out_dir: Path | str | None = None, keep_original_track: bool = True,
        provider: DubProvider | None = None, poll_s: float = POLL_S, timeout_s: float = TIMEOUT_S,
        progress: Callable[[str], None] | None = None) -> DubResult:
    if not allow_cloud:
        raise DubError("dubbing uploads the sound to ElevenLabs (billed) — allow cloud processing for this run")
    path = Path(src)
    if not path.is_file():
        raise DubError(f"no such file: {path}")
    langs = [lang.strip().lower() for lang in languages if lang.strip()]
    if not langs:
        raise DubError("name at least one target language (e.g. fr, es, de, ja)")
    is_video = path.suffix.lower() in VIDEO_EXTS
    if not _has_audio(path):
        raise DubError("the file has no sound to dub")
    from navig_audio.mastering.master import unique_path

    prov = provider or ElevenLabsDubbing()
    dest = Path(out_dir) if out_dir else path.parent
    dest.mkdir(parents=True, exist_ok=True)
    src_dur = _duration(path)
    result = DubResult(str(path))
    with tempfile.TemporaryDirectory(prefix="navig-dub-") as td:
        tmp = Path(td)
        upload = path
        if is_video:  # the picture never leaves the machine
            # MP3: the dubbing endpoint refuses the audio/m4a type an AAC upload is sent as.
            upload = tmp / "sound.mp3"
            _ffmpeg("-i", str(path), "-vn", "-map", "0:a:0", "-c:a", "libmp3lame", "-b:a", "256k", str(upload))

        async def one(lang: str) -> bytes:
            dubbing_id = await prov.start(upload, lang, source_lang, num_speakers)
            await _wait(prov, dubbing_id, poll_s, timeout_s, (lambda s: progress(f"{lang}: {s}")) if progress else None)
            return await prov.download(dubbing_id, lang)

        async def all_langs() -> list[bytes | BaseException]:
            return await asyncio.gather(*(one(lang) for lang in langs), return_exceptions=True)

        blobs = asyncio.run(all_langs())
        for lang, blob in zip(langs, blobs):
            if isinstance(blob, BaseException):
                result.failed[lang] = str(blob)
                continue
            raw = tmp / f"dub.{lang}.bin"
            raw.write_bytes(blob)
            try:
                if is_video:
                    dst = unique_path(dest / f"{path.stem}.{lang}{path.suffix}")
                    maps = ["-map", "0:v", "-map", "1:a:0"] + (["-map", "0:a:0"] if keep_original_track else [])
                    meta = ["-metadata:s:a:0", f"language={ISO639_2.get(lang, lang)}", "-disposition:a:0", "default"]
                    if keep_original_track:
                        meta += ["-metadata:s:a:1", "title=original", "-disposition:a:1", "0"]
                    _ffmpeg("-i", str(path), "-i", str(raw), *maps, "-map", "0:s?", "-c:v", "copy", "-c:s", "copy",
                            "-c:a", "aac", "-b:a", "256k", *meta, str(dst))
                else:
                    dst = unique_path(dest / f"{path.stem}.{lang}.wav")
                    _ffmpeg("-i", str(raw), "-vn", "-c:a", "pcm_s24le", str(dst))
            except DubError as exc:
                result.failed[lang] = str(exc)
                continue
            dur = _duration(dst)
            result.takes.append(DubTake(lang, str(dst), round(dur, 3), round((dur - src_dur) / max(src_dur, 1e-6), 4)))
    if not result.takes:
        return result
    sidecar = unique_path(dest / f"{path.stem}.dub.json")
    sidecar.write_text(json.dumps({**result.to_dict(), "note": "AI-generated speech: a translated dub in cloned voices.",
                                   "provider": "elevenlabs-dubbing", "source_duration_s": src_dur}, indent=2,
                                  ensure_ascii=False), encoding="utf-8")
    result.sidecar = str(sidecar)
    return result
