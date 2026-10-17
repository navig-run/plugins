"""``navig video cut`` — render an EDL (the editable timeline) into its outputs.

  navig video cut edit-decision-list.json
  navig video cut path/to/edl.json --force --json

The EDL is plan-21's timeline format: sources with paths, outputs made of
``{src, t0, t1}`` segments, per-output audio target and optional caption burn.
Editing the file and re-running IS the edit loop — sources are never touched,
intermediates live in ``_cut/`` beside the EDL, and outputs are skipped when
they already exist (``--force`` re-renders).

Mounted on ``video_app`` by import side-effect from ``video.py``, like ``parity``.
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Any

import typer
from navig_sdk import console as ch
from navig_video._typed_path import resolve_user_path

from navig_video.commands.video import video_app

CMD = "navig video cut"


def _media_or_exit():
    """The shared media engine is an extra — say so cleanly instead of a traceback."""
    try:
        from navig_media_core.media.video_edit import (  # noqa: PLC0415
            VideoEditError,
            burn_captions,
            excerpt,
            join,
            master,
            probe,
        )
    except ImportError as exc:
        ch.error(
            "cut needs the shared media engine",
            'install it with: pip install "navig-video[render]"',
        )
        raise typer.Exit(1) from exc
    return VideoEditError, burn_captions, excerpt, join, master, probe


class EdlError(ValueError):
    """The EDL is not renderable — a usage problem, not an ffmpeg failure."""


def _load_edl(path: Path) -> dict[str, Any]:
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EdlError(f"EDL not readable: {exc}") from exc
    except ValueError as exc:
        raise EdlError(f"EDL is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise EdlError(f"EDL must be a JSON object, got {type(data).__name__}")
    outputs = data.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise EdlError("EDL has no outputs[] to render")
    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        raise EdlError("EDL has no sources[] — segments have nothing to cut from")
    return data


def _source_map(edl: dict[str, Any], base: Path) -> dict[str, Path]:
    """id -> absolute path; relative paths resolve beside the EDL."""
    out: dict[str, Path] = {}
    for s in edl["sources"]:
        if not isinstance(s, dict) or "id" not in s or "path" not in s:
            raise EdlError("every source needs {id, path}")
        p = Path(str(s["path"]))
        out[str(s["id"])] = p if p.is_absolute() else (base / p)
    return out


def _check_segments(edl: dict[str, Any], sources: dict[str, Path], probe) -> dict[str, float]:
    """Validate every segment against its probed source; return id -> duration."""
    durations: dict[str, float] = {}
    for out in edl["outputs"]:
        if not isinstance(out, dict) or "id" not in out or "file" not in out:
            raise EdlError("every output needs {id, file, segments}")
        segs = out.get("segments")
        if not isinstance(segs, list) or not segs:
            raise EdlError(f"output {out.get('id')!r} has no segments[]")
        for i, seg in enumerate(segs):
            if not isinstance(seg, dict) or not {"src", "t0", "t1"} <= set(seg):
                raise EdlError(f"output {out['id']!r} segment {i} needs src, t0, t1")
            sid = str(seg["src"])
            if sid not in sources:
                raise EdlError(
                    f"output {out['id']!r} segment {i} names unknown source {sid!r}"
                )
            src = sources[sid]
            if not src.is_file():
                raise EdlError(f"source file missing: {src}")
            if sid not in durations:
                durations[sid] = float(probe(src)["duration"])
            t0, t1 = float(seg["t0"]), float(seg["t1"])
            if t0 < 0 or t1 <= t0:
                raise EdlError(
                    f"output {out['id']!r} segment {i} has empty bounds [{t0}, {t1})"
                )
            if t1 > durations[sid] + 0.05:
                raise EdlError(
                    f"output {out['id']!r} segment {i} ends at {t1:g}s but "
                    f"{sid} is {durations[sid]:.3f}s"
                )
    return durations


@video_app.command("cut")
def video_cut(
    edl: Path = typer.Argument(..., help="EDL JSON: sources[] + outputs[] of {src,t0,t1} segments."),
    force: bool = typer.Option(False, "--force", help="Re-render outputs that already exist."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable result."),
) -> None:
    """🎬 Render an EDL — the editable timeline — into its outputs.

    Non-destructive by construction: sources are read-only, intermediates land in
    ``_cut/`` beside the EDL, existing outputs are kept unless ``--force``. Audio
    is mastered to each output's ``audio`` target, captions burn when the output
    says so.
    """
    VideoEditError, burn_captions, excerpt, join, master, probe = _media_or_exit()
    edl_path = resolve_user_path(edl)

    try:
        data = _load_edl(edl_path)
        base = edl_path.parent
        sources = _source_map(data, base)
        _check_segments(data, sources, probe)
    except EdlError as exc:
        ch.error("EDL not renderable", str(exc))
        raise typer.Exit(2) from exc

    work = base / "_cut"
    results: list[dict[str, Any]] = []
    try:
        for out in data["outputs"]:
            dst = Path(str(out["file"]))
            if not dst.is_absolute():
                dst = base / dst
            if dst.is_file() and not force:
                results.append({
                    "id": out["id"], "file": str(dst), "cached": True,
                    "bytes": dst.stat().st_size,
                })
                continue
            segs = out["segments"]
            parts: list[Path] = []
            fps = 30
            for i, seg in enumerate(segs):
                src = sources[str(seg["src"])]
                part = work / f"{out['id']}-{i}.mp4"
                encode = out.get("encode") or {}
                excerpt(
                    src, part, float(seg["t0"]), float(seg["t1"]),
                    crf=int(encode.get("crf", 18)),
                    maxrate=encode.get("maxrate"),
                )
                parts.append(part)
                if i == 0:
                    fps = max(1, int(round(float(probe(src)["fps"]))))
            if len(parts) == 1:
                joined = parts[0]
            else:
                joined = work / f"{out['id']}-joined.mp4"
                join(parts, joined, transition="cut", fps=fps)
            audio = out.get("audio") or {}
            cap = out.get("captions")
            if isinstance(cap, dict) and cap.get("burn"):
                srt = Path(str(cap["srt"]))
                if not srt.is_absolute():
                    srt = base / srt
                if not srt.is_file():
                    raise EdlError(f"caption file missing for {out['id']!r}: {srt}")
                loud = work / f"{out['id']}-loud.mp4"
                master(joined, loud, i=float(audio.get("i", -14.0)),
                       tp=float(audio.get("tp", -1.0)))
                burn_captions(loud, srt, dst)
            else:
                master(joined, dst, i=float(audio.get("i", -14.0)),
                       tp=float(audio.get("tp", -1.0)))
            results.append({
                "id": out["id"], "file": str(dst), "cached": False,
                "bytes": dst.stat().st_size,
            })
    except EdlError as exc:
        ch.error("EDL not renderable", str(exc))
        raise typer.Exit(2) from exc
    except VideoEditError as exc:
        ch.error("Render failed", str(exc))
        raise typer.Exit(1) from exc

    if as_json:
        ch.raw_print(_json.dumps({"edl": str(edl_path), "outputs": results},
                                 indent=2, ensure_ascii=False))
        return
    for r in results:
        state = "cached" if r["cached"] else "rendered"
        ch.success(f"Cut {r['id']}", f"{state} · {r['bytes'] // 1024} KB · {r['file']}")
