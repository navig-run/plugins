"""Describe a recording in words — the prompt a regeneration starts from.

Two sources, kept apart so a reader can tell them apart:

* **measured** — always: category, length, how many distinct events and when, how
  bright or dark, where the energy sits. Comes from :mod:`navig_audio.enhance.analysis`;
  nothing leaves the machine.
* **heard** — only with ``use_ai=True``: an audio-capable model (via ``navig_sdk.ai``)
  listens and names what it hears ("coins clink, a drawer slides, a bell rings"). This
  UPLOADS the clip, so it is opt-in per run, and its words are a hint: a model can mishear.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from navig_audio.enhance.analysis import AudioReport

logger = logging.getLogger(__name__)

AI_MODEL = "google/gemini-2.5-flash"
AI_PROVIDER = "openrouter"
MAX_AI_UPLOAD_BYTES = 8_000_000


@dataclass
class Description:
    measured: str
    heard: str = ""
    heard_by: str = ""

    @property
    def text(self) -> str:
        return f"{self.heard.strip()} ({self.measured})" if self.heard.strip() else self.measured

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["text"] = self.text
        return d


def _tone(report: AudioReport) -> str:
    c = report.spectral_centroid_hz
    if c >= 4000:
        return "bright, metallic or airy"
    if c >= 1800:
        return "clear, mid-forward"
    if c >= 700:
        return "warm"
    return "dark, bass-heavy"


def measured_description(report: AudioReport) -> str:
    """A plain sentence from the measurements alone (deterministic, local)."""
    n = len(report.onsets_s)
    dur = report.decoded_duration_s
    kind = {"sfx": "sound effect", "speech": "spoken voice", "music": "music", "ambience": "ambient bed"}.get(
        report.category, "audio")
    events = (
        f"{n} distinct hits over {dur:.1f} s" if report.category == "sfx" and n
        else f"{n} events in {dur:.1f} s" if n else f"{dur:.1f} s, sustained"
    )
    parts = [f"{kind}, {events}", _tone(report)]
    if report.channels >= 2:
        parts.append("stereo")
    return ", ".join(parts)


def ai_description(path: Path, *, timeout: float = 90) -> str:
    """Ask an audio-capable model what it hears. UPLOADS the file — call only on opt-in."""
    from navig_sdk import ai

    data = path.read_bytes()
    if len(data) > MAX_AI_UPLOAD_BYTES:
        raise ValueError(f"{path.name} is too large to describe with AI (limit {MAX_AI_UPLOAD_BYTES // 1_000_000} MB)")
    fmt = path.suffix.lower().lstrip(".") or "wav"
    messages = [{"role": "user", "content": [
        {"type": "text", "text": (
            "You are helping a sound designer. In ONE sentence of at most 30 words, say what this "
            "recording is and list its audible events in order, with their character. No preamble."
        )},
        {"type": "input_audio", "input_audio": {"data": base64.b64encode(data).decode("ascii"), "format": fmt}},
    ]}]
    text = ai.generate(messages, provider=AI_PROVIDER, model=AI_MODEL, max_tokens=120, timeout=timeout)
    return " ".join(str(text).split())[:400]


def describe(report: AudioReport, *, use_ai: bool = False) -> Description:
    d = Description(measured=measured_description(report))
    if use_ai:
        d.heard = ai_description(Path(report.path))
        d.heard_by = f"{AI_PROVIDER}/{AI_MODEL}"
    return d
