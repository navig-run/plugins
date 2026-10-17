"""`navig obs clips` — the Grid Pigeon's moments, cut into vertical clips.

    navig obs clips                      # the newest session that has moments
    navig obs clips --session <dir>      # that one
    navig obs clips --dry-run            # the plan: windows, kinds, captions
    navig obs clips --focus 0.5          # crop around the middle instead

Moments are written by the gateway while OBS records (the pigeon page reports
them; see navig_obs.show.moments). Nothing here talks to OBS.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from navig_obs.commands._common import console, emit, fail, notice, ok
from navig_obs.show import clips as clips_mod
from navig_obs.show import moments


def _newest_session(root: Path) -> Path | None:
    best: tuple[float, Path] | None = None
    if not root.is_dir():
        return None
    for d in root.iterdir():
        m = d / "markers.jsonl"
        if not (d / "session.json").is_file() or not m.is_file():
            continue
        if not any(r.get("label", "").startswith("pigeon-") for r in clips_mod.read_jsonl(m)):
            continue
        t = (d / "session.json").stat().st_mtime
        if best is None or t > best[0]:
            best = (t, d)
    return best[1] if best else None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def clips(
    session: Annotated[
        str | None, typer.Option("--session", help="Session dir (default: the newest with pigeon moments).")
    ] = None,
    recording: Annotated[
        str | None, typer.Option("--recording", help="The recording file (default: found from the session).")
    ] = None,
    out: Annotated[str | None, typer.Option("--out", help="Output dir (default: <session>/clips).")] = None,
    focus: Annotated[
        float, typer.Option("--focus", min=0.0, max=1.0, help="The pigeon's x on the canvas, 0..1 (crop centre).")
    ] = 0.82,
    layout: Annotated[
        str, typer.Option("--layout", help="square (him + bubble + terminal, blurred fill) | tall (a 9:16 slice).")
    ] = "square",
    captions: Annotated[
        bool, typer.Option("--captions/--no-captions", help="Burn his lines in, large, for muted viewers.")
    ] = True,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print the plan; cut nothing.")] = False,
    force: Annotated[bool, typer.Option("--force", help="Re-render clips that already exist.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Cut the Grid Pigeon's moments from a recording into 9:16 clips, captions burned in."""
    if layout not in clips_mod.LAYOUTS:
        fail(f"--layout must be one of {', '.join(clips_mod.LAYOUTS)}", 2)
    root = moments.sessions_root()
    sdir = Path(session).expanduser() if session else _newest_session(root)
    if sdir is None or not (sdir / "session.json").is_file():
        fail(f"no session with pigeon moments under {root} — record with the pigeon on stage first", 2)
    record = _read_json(sdir / "session.json")
    meta = _read_json(sdir / moments.SIDE_META)
    markers = clips_mod.read_jsonl(sdir / record.get("markersFile", "markers.jsonl"))
    lines = clips_mod.read_jsonl(sdir / moments.SIDE_LINES)

    rec = Path(recording).expanduser() if recording else clips_mod.find_recording(sdir, record, meta)
    duration = None
    if rec and rec.is_file() and not dry_run:
        try:
            from navig_media_core.media.video_edit import probe  # noqa: PLC0415

            duration = float(probe(rec)["duration"])
        except ImportError:
            fail('clips need the shared media engine: pip install "navig-video[render]"')
    plan = clips_mod.plan(markers, lines, duration)

    if dry_run or not plan:
        data = {
            "session": str(sdir), "recording": str(rec) if rec else None,
            "moments": sum(1 for m in markers if str(m.get("label", "")).startswith("pigeon-")),
            "lines": len(lines), "clips": [c.to_json() for c in plan],
        }
        if as_json:
            emit(data, as_json=True)
            return
        if not plan:
            notice(f"no pigeon moments in {sdir}")
            return
        for i, c in enumerate(plan, 1):
            console.print(f"{i:>2}. {c.t0:7.1f}s → {c.t1:7.1f}s  {c.name}  "
                          f"({len(c.captions)} caption{'s' if len(c.captions) != 1 else ''})")
        notice(f"recording: {rec or '— not found (pass --recording)'}")
        return

    if rec is None or not rec.is_file():
        fail("the recording is not found — pass --recording <file>")
    if not record.get("recordingPath"):
        record["recordingPath"] = str(rec)  # contract 7 has the field: fill it
        try:
            moments._write_json(sdir / "session.json", record)  # noqa: SLF001
        except OSError:
            pass
    out_dir = Path(out).expanduser() if out else sdir / "clips"
    try:
        results = clips_mod.render(
            rec, plan, out_dir, prefix=str(record.get("sessionId") or sdir.name),
            focus=focus, force=force, layout=layout, captions=captions,
        )
    except ImportError:
        fail('clips need the shared media engine: pip install "navig-video[render]"')
    except Exception as exc:  # noqa: BLE001 — ffmpeg's last words are the useful part
        fail(f"render failed: {exc}")
    (out_dir / "clips.json").write_text(
        json.dumps({"session": str(sdir), "recording": str(rec), "clips": results},
                   indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if as_json:
        emit({"session": str(sdir), "out": str(out_dir), "clips": results}, as_json=True)
        return
    for r in results:
        state = "cached" if r.get("cached") else "cut"
        ok(f"{state}  {Path(r['file']).name}  ({r['t1'] - r['t0']:.1f}s, {len(r['captions'])} captions)")
    notice(f"{len(results)} clip(s) in {out_dir}")
