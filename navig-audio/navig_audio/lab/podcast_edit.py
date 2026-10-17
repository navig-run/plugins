"""Podcast auto-edit: fillers, long pauses and stutters out — from a reviewable cut list.

1. **Transcribe locally** (faster-whisper, word timestamps; nothing leaves the machine).
   Whisper tends to tidy fillers away, so it is prompted with a disfluent example —
   still, a filler it does not write down cannot be cut; the report says so.
2. **Propose cuts**: filler words (per language), pauses longer than ``max_pause``
   (shortened to ``keep_pause``, never removed), and immediate repeats ("the the").
3. **Review**: the cut list is a plain JSON file; set ``"keep": true`` on any cut to
   keep it, then apply that file.
4. **Apply**: kept segments are joined with 15 ms equal-power crossfades (no clicks);
   the original is never written; the report says how much time was saved.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

FILLERS = {
    "en": {"um", "umm", "uh", "uhh", "erm", "er", "ah", "hmm", "mm", "mhm", "uhm"},
    "fr": {"euh", "heu", "bah", "ben", "hum", "hein"},
    "ru": {"э", "ээ", "эм", "мм", "ну-у"},
    "de": {"äh", "ähm", "öh", "hm"},
    "es": {"eh", "em", "este", "mmm"},
}
# Whisper mirrors the style of its prompt: a disfluent one keeps disfluencies in.
DISFLUENT_PROMPT = "Umm, so, uh, I was, like, thinking — hmm — the the idea is, uh, simple."
MODEL = "small"
XFADE_S = 0.015


class PodcastEditError(RuntimeError):
    """Transcription unavailable, bad cut list, or ffmpeg failure."""


@dataclass
class Cut:
    start: float
    end: float
    kind: str  # filler · pause · repeat
    text: str
    keep: bool = False

    @property
    def seconds(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class CutList:
    source: str
    language: str
    duration_s: float
    cuts: list[Cut] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["removable_s"] = round(sum(c.seconds for c in self.cuts if not c.keep), 2)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CutList:
        try:
            cuts = [Cut(float(c["start"]), float(c["end"]), str(c["kind"]), str(c.get("text", "")),
                        bool(c.get("keep", False))) for c in d.get("cuts", [])]
            return cls(str(d["source"]), str(d.get("language", "")), float(d["duration_s"]), cuts, str(d.get("note", "")))
        except (KeyError, TypeError, ValueError) as exc:
            raise PodcastEditError(f"not a valid cut list: {exc}") from exc


def transcribe_words(path: Path, *, model: str = MODEL, language: str | None = None) -> tuple[list[dict], str, float]:
    """[{word, start, end}], detected language, duration — local faster-whisper."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise PodcastEditError("local transcription needs faster-whisper (pip install faster-whisper)") from exc
    try:
        import ctranslate2

        device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:  # noqa: BLE001 — no CUDA runtime: CPU is fine
        device = "cpu"
    wm = WhisperModel(model, device=device, compute_type="float16" if device == "cuda" else "int8")
    segs, info = wm.transcribe(str(path), word_timestamps=True, language=language, vad_filter=False,
                               initial_prompt=DISFLUENT_PROMPT, condition_on_previous_text=False)
    words = []
    for s in segs:
        for w in s.words or []:
            words.append({"word": w.word.strip(), "start": float(w.start), "end": float(w.end)})
    return words, info.language or (language or ""), float(info.duration or 0.0)


def _norm(w: str) -> str:
    return re.sub(r"[^\w\-]+", "", w.lower())


def propose_cuts(words: list[dict], language: str, duration: float, *, max_pause: float = 1.2,
                 keep_pause: float = 0.5, cut_fillers: bool = True, cut_repeats: bool = True) -> list[Cut]:
    fillers = FILLERS.get(language[:2], set()) | FILLERS["en"]
    cuts: list[Cut] = []
    for i, w in enumerate(words):
        token = _norm(w["word"])
        if cut_fillers and token in fillers:
            # Take the filler and the breath right after it, but not into the next word.
            nxt = words[i + 1]["start"] if i + 1 < len(words) else w["end"]
            cuts.append(Cut(round(w["start"], 3), round(min(nxt, w["end"] + 0.15), 3), "filler", w["word"]))
            continue
        if cut_repeats and i + 1 < len(words) and token and token == _norm(words[i + 1]["word"]) and len(token) > 1:
            cuts.append(Cut(round(w["start"], 3), round(words[i + 1]["start"], 3), "repeat", f"{w['word']} {words[i + 1]['word']}"))
    for a, b in zip(words, words[1:]):
        gap = b["start"] - a["end"]
        if gap > max_pause:
            mid = a["end"] + keep_pause / 2
            cuts.append(Cut(round(mid, 3), round(b["start"] - keep_pause / 2, 3), "pause", f"{gap:.1f}s pause"))
    cuts = [c for c in cuts if c.seconds >= 0.05]
    cuts.sort(key=lambda c: c.start)
    merged: list[Cut] = []
    for c in cuts:  # overlaps merge so nothing is cut twice
        if merged and c.start <= merged[-1].end:
            merged[-1].end = max(merged[-1].end, c.end)
            merged[-1].text += f" + {c.text}"
        else:
            merged.append(c)
    return merged


def analyse(src: Path | str, *, model: str = MODEL, language: str | None = None, **kw: Any) -> CutList:
    path = Path(src)
    words, lang, dur = transcribe_words(path, model=model, language=language)
    cuts = propose_cuts(words, lang, dur, **kw)
    note = ("Fillers are found only where the transcript wrote them down; listen to the result."
            if words else "No speech was recognised — nothing to cut.")
    return CutList(str(path), lang, dur, cuts, note)


def _decode(path: Path) -> tuple[np.ndarray, int, int]:
    from navig_audio.enhance.analysis import decode, probe

    info = probe(path)
    return decode(path, info["sample_rate"], info["channels"]), info["sample_rate"], info["channels"]


def apply_cuts(cutlist: CutList, *, out_dir: Path | str | None = None) -> tuple[Path, float]:
    """Join the kept audio with short crossfades. Returns (file, seconds removed)."""
    from navig_audio.enhance.engine import output_name

    src = Path(cutlist.source)
    if not src.is_file():
        raise PodcastEditError(f"the source is gone: {src}")
    x, sr, ch = _decode(src)
    n = x.shape[1]
    active = sorted((c for c in cutlist.cuts if not c.keep), key=lambda c: c.start)
    keep_ranges, pos = [], 0
    for c in active:
        a, b = int(c.start * sr), int(c.end * sr)
        if a > pos:
            keep_ranges.append((pos, min(a, n)))
        pos = max(pos, b)
    if pos < n:
        keep_ranges.append((pos, n))
    xf = int(XFADE_S * sr)
    t = np.linspace(0, np.pi / 2, xf) if xf > 1 else np.zeros(0)
    fin, fout = np.sin(t) ** 2, np.cos(t) ** 2
    out = x[:, keep_ranges[0][0]:keep_ranges[0][1]].copy() if keep_ranges else np.zeros((ch, 0), np.float32)
    for a, b in keep_ranges[1:]:
        seg = x[:, a:b]
        if xf > 1 and out.shape[1] >= xf and seg.shape[1] >= xf:
            out[:, -xf:] = out[:, -xf:] * fout + seg[:, :xf] * fin
            out = np.concatenate([out, seg[:, xf:]], axis=1)
        else:
            out = np.concatenate([out, seg], axis=1)
    dest = Path(out_dir) if out_dir else src.parent
    dest.mkdir(parents=True, exist_ok=True)
    dst = output_name(src, dest, ".wav", "edited")
    exe = shutil.which("ffmpeg")
    if not exe:
        raise PodcastEditError("ffmpeg is not on PATH")
    proc = subprocess.run([exe, "-hide_banner", "-y", "-v", "error", "-f", "f32le", "-ar", str(sr), "-ac", str(ch),  # noqa: S603
                           "-i", "-", "-c:a", "pcm_s24le", str(dst)],
                          input=np.clip(out, -1, 1).T.astype("<f4").tobytes(), capture_output=True, check=False)
    if proc.returncode != 0:
        dst.unlink(missing_ok=True)
        raise PodcastEditError("ffmpeg could not write the edited file")
    removed = (n - out.shape[1]) / sr
    (dst.parent / (dst.name + ".cuts.json")).write_text(json.dumps(cutlist.to_dict(), indent=2, ensure_ascii=False),
                                                         encoding="utf-8")
    return dst, removed
