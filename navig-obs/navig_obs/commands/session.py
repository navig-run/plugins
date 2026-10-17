"""`navig obs session` + `navig obs marker` — one recording, one on-disk record.

A *session* is the durable record of one recording: `session.json` anchors the
recording clock (startEpoch + scene + collection + canvas), `markers.jsonl`
appends events with their `tOffset` (seconds into the recording). This is the
writer of SCHEMA's broadcast **contract 7**
(`design/broadcast/contracts/contract-07-markers.schema.json`); the shapes are
frozen there — change the contract before changing the writer.

Journey:

    navig obs record start
    navig obs session start --lang fr        # prints the session dir
    navig obs marker pigeon-cue              # --session or NAVIG_OBS_SESSION
    navig obs record stop
    navig obs session stop                   # closes the record, finds the file

Sessions default under ``~/.navig/obs-sessions/<sessionId>/`` (media never
enters a repository); ``--dir`` / ``NAVIG_OBS_SESSIONS`` point elsewhere.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from navig_obs.commands._common import console, emit, fail, kv_table, notice, ok, request

session_app = typer.Typer(
    name="session",
    help="Recording sessions: session.json + markers.jsonl (the recording clock).",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

KINDS = ("cue", "character", "scene", "mission", "anomaly", "bookmark", "incident")
VIDEO_SUFFIXES = (".mp4", ".mkv", ".mov", ".ts", ".flv", ".m4v")
CONTRACT_VERSION = 1


def _sessions_root(dir_opt: str | None) -> Path:
    base = dir_opt or os.environ.get("NAVIG_OBS_SESSIONS")
    if base:
        return Path(base).expanduser()
    return Path.home() / ".navig" / "obs-sessions"


def _session_dir_from_env(session_opt: str | None) -> Path:
    raw = session_opt or os.environ.get("NAVIG_OBS_SESSION")
    if not raw:
        fail(
            "no session given — pass --session <dir>, set NAVIG_OBS_SESSION, "
            "or start one with `navig obs session start`",
            2,
        )
    return Path(raw).expanduser()


def _load_session(session_dir: Path) -> dict[str, Any]:
    path = session_dir / "session.json"
    if not path.is_file():
        fail(f"no session.json in {session_dir} — `navig obs session start` first")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"unreadable session record {path}: {exc}")
    if data.get("contractVersion") != CONTRACT_VERSION:
        fail(f"session record is contract v{data.get('contractVersion')}, this command speaks v{CONTRACT_VERSION}")
    return data


def _save_session(session_dir: Path, data: dict[str, Any]) -> None:
    path = session_dir / "session.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _infer_lang(collection: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    m = re.search(r"\((en|fr|ru)\)", collection)
    return m.group(1) if m else "en"


# ── session start ───────────────────────────────────────────────────────────
@session_app.command("start")
def session_start(
    dir_opt: Annotated[
        str | None,
        typer.Option("--dir", help="Session directory (default: ~/.navig/obs-sessions/<id>)."),
    ] = None,
    lang: Annotated[str | None, typer.Option("--lang", help="Show language: en|fr|ru (default: from collection name).")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Open a session: anchor the recording clock with scene, collection and canvas."""
    if lang is not None and lang not in ("en", "fr", "ru"):
        fail(f"--lang must be en, fr or ru (got {lang!r})", 2)

    scene = (request("GetCurrentProgramScene") or {}).get("currentProgramSceneName", "")
    collection = (request("GetCurrentSceneCollection") or {}).get("currentSceneCollectionName", "")
    video = request("GetVideoInfo") or {}
    base_cx = int(video.get("baseCX") or 0)
    base_cy = int(video.get("baseCY") or 0)

    session_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    session_dir = _sessions_root(dir_opt) / session_id
    try:
        session_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        fail(f"session dir already exists: {session_dir}")

    record: dict[str, Any] = {
        "contractVersion": CONTRACT_VERSION,
        "sessionId": session_id,
        "startEpoch": int(time.time() * 1000),
        "scene": scene,
        "collection": collection,
        "lang": _infer_lang(collection, lang),
        "portrait": base_cx == 1080 and base_cy == 1920,
        "recordingPath": None,
        "markersFile": "markers.jsonl",
        "endEpoch": None,
        "showId": None,
    }
    _save_session(session_dir, record)
    (session_dir / "markers.jsonl").write_text("", encoding="utf-8")

    if as_json:
        emit({"session": str(session_dir), **record}, as_json=True)
        return
    ok(f"session {session_id} → {session_dir}")
    console.print(
        kv_table(
            "session",
            [
                ("scene", scene or "—"),
                ("collection", collection or "—"),
                ("lang", record["lang"]),
                ("canvas", f"{base_cx}x{base_cy}" + (" (portrait)" if record["portrait"] else "")),
            ],
        )
    )
    notice(f"next: navig obs marker <label> --session \"{session_dir}\"")


# ── marker ──────────────────────────────────────────────────────────────────
def marker(
    label: Annotated[str, typer.Argument(help="Marker label, e.g. pigeon-cue.")],
    session: Annotated[str | None, typer.Option("--session", help="Session dir (or NAVIG_OBS_SESSION).")] = None,
    kind: Annotated[str, typer.Option("--kind", help=f"One of: {', '.join(KINDS)}.")] = "bookmark",
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Append a marker to the session's recording clock (OBS must be recording)."""
    label = label.strip()
    if not label:
        fail("marker label must not be empty", 2)
    if kind not in KINDS:
        fail(f"--kind must be one of: {', '.join(KINDS)} (got {kind!r})", 2)

    session_dir = _session_dir_from_env(session)
    record = _load_session(session_dir)

    status = request("GetRecordStatus") or {}
    if not status.get("outputActive"):
        fail("OBS is not recording — markers need the recording clock (`navig obs record start` first)")
    t_offset = int(status.get("outputDuration") or 0) / 1000.0

    line = {
        "contractVersion": CONTRACT_VERSION,
        "tOffset": round(t_offset, 3),
        "wallClock": int(time.time() * 1000),
        "label": label,
        "kind": kind,
    }
    markers_path = session_dir / record.get("markersFile", "markers.jsonl")
    with markers_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")

    if as_json:
        emit(line, as_json=True)
        return
    ok(f"marker @{t_offset:.1f}s  {label} ({kind})")


# ── session stop ────────────────────────────────────────────────────────────
@session_app.command("stop")
def session_stop(
    session: Annotated[str | None, typer.Option("--session", help="Session dir (or NAVIG_OBS_SESSION).")] = None,
    recording: Annotated[
        str | None,
        typer.Option("--recording", help="Recording file path (default: newest file since startEpoch)."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Close the session: endEpoch + the recording file, if one can be found."""
    session_dir = _session_dir_from_env(session)
    record = _load_session(session_dir)

    status = request("GetRecordStatus") or {}
    if status.get("outputActive"):
        fail("still recording — `navig obs record stop` first, then close the session")

    record["endEpoch"] = int(time.time() * 1000)

    if recording:
        record["recordingPath"] = str(Path(recording).expanduser())
    elif not record.get("recordingPath"):
        found = _find_recording(int(record["startEpoch"]))
        if found:
            record["recordingPath"] = found

    _save_session(session_dir, record)

    markers_path = session_dir / record.get("markersFile", "markers.jsonl")
    marker_count = 0
    if markers_path.is_file():
        marker_count = sum(1 for ln in markers_path.read_text(encoding="utf-8").splitlines() if ln.strip())

    duration_s = (record["endEpoch"] - record["startEpoch"]) / 1000.0
    if as_json:
        emit({"session": str(session_dir), "markers": marker_count, **record}, as_json=True)
        return
    ok(f"session {record['sessionId']} closed after {duration_s:.0f}s, {marker_count} marker(s)")
    console.print(
        kv_table(
            "session",
            [
                ("dir", str(session_dir)),
                ("recording", record.get("recordingPath") or "—"),
                ("markers", str(marker_count)),
            ],
        )
    )


def _find_recording(start_epoch_ms: int) -> str | None:
    """Newest video file written at/after the session's start (best effort)."""
    directory = (request("GetRecordDirectory") or {}).get("recordDirectory")
    if not directory:
        return None
    root = Path(directory)
    if not root.is_dir():
        return None
    cutoff = (start_epoch_ms - 5000) / 1000.0  # 5 s of clock skew head-room
    candidates = [
        p
        for p in root.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES and p.stat().st_mtime >= cutoff
    ]
    if not candidates:
        return None
    return str(max(candidates, key=lambda p: p.stat().st_mtime))
