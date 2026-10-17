"""What gets written next to a rendered beat, and the post-check that keeps it honest.

A beat without its prompt is a file nobody can reproduce or extend. Every render leaves
a JSON sidecar (style, plan, model, seed, target and *measured* tempo/key) and a row in
the folder's ``INDEX.md``, so a keeper can be re-rendered, a miss can be diagnosed, and
the folder reads as a catalogue rather than a pile of mp3s.

The measurement is the part that matters: a music model asked for 76 bpm can return 80
without saying so, and a rapper who wrote to 76 finds out on the first take.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

# A render is "on tempo" when the measured beat is within this of the target, once
# half- and double-time readings have been folded — 3% is about the difference between
# 76 and 78, which a rapper still rides; past it the count stops fitting.
BPM_TOLERANCE = 0.03

INDEX_HEADER = (
    "| Created | File | Style | BPM (target → measured) | Key (target → measured) | Model | Prompt |\n"
    "|---|---|---|---|---|---|---|\n"
)


def tempo_relation(target: float, measured: float, *, tolerance: float = BPM_TOLERANCE) -> str | None:
    """How a measured tempo relates to the target: ``same``, ``half/double``, ``triplet`` or None.

    ``triplet`` is a 2/3 or 3/2 reading: rattles, shakers and 6/8 grooves pull the detector onto
    the dotted pulse (measured: a 92 bpm beat read as 61.5, a 150 bpm one as 99.5). The render
    is on tempo; only the reading moved, so it must not be flagged as a miss.
    """
    if target <= 0 or measured <= 0:
        return None
    for label, factors in (("same", (1.0,)), ("half/double", (0.5, 2.0)), ("triplet", (1.5, 2 / 3, 0.75, 3.0))):
        if any(abs(measured * k - target) / target <= tolerance for k in factors):
            return label
    return None


def tempo_matches(target: float, measured: float, *, tolerance: float = BPM_TOLERANCE) -> bool:
    """True when ``measured`` is ``target`` at 1×, ½×, 2× or a triplet reading, within tolerance."""
    return tempo_relation(target, measured, tolerance=tolerance) is not None


def measure(path: Path, *, target_bpm: float | None = None) -> dict[str, Any]:
    """Tempo, key and loudness of a rendered file — or the reason it could not be read."""
    try:
        from navig_generate.media.tonality import profile
    except ImportError as exc:  # an older core without the profiler
        return {"error": f"measurement unavailable: {exc}"}
    try:
        p = profile(path)
    except Exception as exc:  # noqa: BLE001 — a bad render is reported, not raised
        return {"error": str(exc)}
    out: dict[str, Any] = {
        "bpm": round(p.bpm, 2),
        "bpm_felt": round(p.bpm_felt, 2),
        "beat_confidence": round(p.beat_confidence, 3),
        "key": p.key.name,
        "key_confidence": round(p.key.confidence, 3),
        "bass_root": p.key.bass_root,
        "loudness_dbfs": round(p.loudness_dbfs, 1),
        "duration_s": round(p.duration_s, 2),
        "bands": {k: round(v, 3) for k, v in p.bands.items()},
    }
    if target_bpm:
        relation = tempo_relation(float(target_bpm), p.bpm)
        out["bpm_ok"] = relation is not None
        if relation == "triplet":
            out["bpm_note"] = "triplet reading — the render is on tempo, the detector counted the dotted pulse"
        # The arrangement the file actually has — where the full beat enters, where it drops
        # out. A render does not always follow its plan, and lyrics are timed from this.
        try:
            from navig_audio.beats.bars import bar_map

            out["bars"] = bar_map(path, float(target_bpm)).to_dict()
        except Exception as exc:  # noqa: BLE001 — a missing bar map must not lose the render
            out["bars"] = {"error": str(exc)}
    return out


def write_sidecar(audio_path: Path, record: dict[str, Any]) -> Path:
    side = audio_path.with_suffix(".json")
    side.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return side


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def append_index(out_dir: Path, record: dict[str, Any]) -> Path:
    """Append one row to ``INDEX.md`` — created with its header on first use."""
    index = out_dir / "INDEX.md"
    if not index.exists():
        index.write_text("# Beats\n\n" + INDEX_HEADER, encoding="utf-8")
    m = record.get("measured") or {}
    target_bpm = record.get("target", {}).get("bpm")
    target_key = record.get("target", {}).get("key")
    bpm_cell = f"{target_bpm:g} → {m.get('bpm_felt', '—')}" if target_bpm else str(m.get("bpm_felt", "—"))
    if m.get("bpm_ok") is False:
        bpm_cell += " ⚠"
    elif m.get("bpm_note"):
        bpm_cell += " (triplet)"
    key_cell = f"{target_key} → {m.get('key', '—')}" if target_key else str(m.get("key", "—"))
    prompt = record.get("prompt", "")
    if len(prompt) > 120:
        prompt = prompt[:117] + "…"
    row = "| " + " | ".join(_cell(c) for c in (
        record.get("created", ""),
        f"`{Path(record.get('file', '')).name}`",
        record.get("style", ""),
        bpm_cell,
        key_cell,
        record.get("model", ""),
        prompt,
    )) + " |\n"
    with index.open("a", encoding="utf-8") as fh:
        fh.write(row)
    return index


def make_record(
    *,
    audio_path: Path,
    style_id: str,
    prompt: str,
    plan: dict[str, Any] | None,
    model: str | None,
    seed: int | None,
    target_bpm: float,
    target_key: str,
    provider: str = "elevenlabs",
    generation_time: float | None = None,
    measured: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "file": str(audio_path),
        "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "style": style_id,
        "provider": provider,
        "model": model,
        "seed": seed,
        "target": {"bpm": target_bpm, "key": target_key},
        "prompt": prompt,
        "plan": plan,
        "generation_time_s": round(generation_time, 1) if generation_time is not None else None,
        "measured": measured or {},
    }
