"""Session + marker tests — the SCHEMA contract-7 writer, through the real app.

Shapes asserted here mirror design/broadcast/contracts/contract-07-markers
schema.json (SCHEMA repo): sessionId ``YYYYMMDD-HHMMSS-xxxx``, marker fields
``tOffset/wallClock/label/kind``, session anchors ``startEpoch/scene/lang/
portrait/markersFile``. A drift fails HERE before it fails the broadcast gate.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from typer.testing import CliRunner

from navig_obs.commands.obs import obs_app

runner = CliRunner()

SESSION_ID_RE = re.compile(r"^\d{8}-\d{6}-[a-z0-9]{4}$")

CANVAS_PORTRAIT = {
    "GetCurrentProgramScene": {"currentProgramSceneName": "01 Signal Acquisition"},
    "GetCurrentSceneCollection": {"currentSceneCollectionName": "SCHEMA BROADCAST (fr)"},
    "GetVideoInfo": {"baseCX": 1080, "baseCY": 1920, "outputCX": 1080, "outputCY": 1920, "fps": 60.0},
}


def _invoke(args: list[str]):
    return runner.invoke(obs_app, args)


def _start(tmp_path: Path, port: int) -> Path:
    result = _invoke(
        ["--port", str(port), "session", "start", "--json", "--dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    return Path(payload["session"])


# ── session start ───────────────────────────────────────────────────────────
def test_start_writes_contract_valid_record(obs_factory, tmp_path):
    server = obs_factory(requests=dict(CANVAS_PORTRAIT))
    session_dir = _start(tmp_path, server.port)

    record = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    assert record["contractVersion"] == 1
    assert SESSION_ID_RE.match(record["sessionId"]), record["sessionId"]
    assert record["scene"] == "01 Signal Acquisition"
    assert record["collection"] == "SCHEMA BROADCAST (fr)"
    assert record["lang"] == "fr"  # inferred from the collection name
    assert record["portrait"] is True  # 1080x1920 canvas
    assert record["markersFile"] == "markers.jsonl"
    assert record["startEpoch"] <= int(time.time() * 1000)
    assert record["endEpoch"] is None and record["recordingPath"] is None
    assert (session_dir / "markers.jsonl").exists()


def test_start_explicit_lang_wins_and_bad_lang_is_usage(obs_factory, tmp_path):
    server = obs_factory(requests=dict(CANVAS_PORTRAIT))
    session_dir = _start(tmp_path, server.port)
    # (already started above with inferred lang — start a second with --lang ru)
    result = _invoke(
        ["--port", str(server.port), "session", "start", "--json", "--dir", str(tmp_path), "--lang", "ru"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["lang"] == "ru"

    bad = _invoke(
        ["--port", str(server.port), "session", "start", "--dir", str(tmp_path), "--lang", "de"]
    )
    assert bad.exit_code == 2
    assert session_dir.name  # untouched


def test_start_unreachable_obs_exits_one(obs_factory, tmp_path, unused_port):
    _ = obs_factory  # fixture not started — port unused
    result = _invoke(["--port", str(unused_port), "session", "start", "--dir", str(tmp_path)])
    assert result.exit_code == 1


# ── marker ──────────────────────────────────────────────────────────────────
def test_marker_appends_contract_valid_line(obs_factory, tmp_path):
    server = obs_factory(
        requests={
            **CANVAS_PORTRAIT,
            "GetRecordStatus": {"outputActive": True, "outputDuration": 65000, "outputPaused": False},
        }
    )
    session_dir = _start(tmp_path, server.port)

    result = _invoke(
        ["--port", str(server.port), "marker", "pigeon-cue", "--session", str(session_dir), "--kind", "cue", "--json"]
    )
    assert result.exit_code == 0, result.output
    line = json.loads(result.output)
    assert line["contractVersion"] == 1
    assert line["label"] == "pigeon-cue"
    assert line["kind"] == "cue"
    assert line["tOffset"] == 65.0  # seconds from outputDuration
    assert line["wallClock"] <= int(time.time() * 1000)

    on_disk = (session_dir / "markers.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(on_disk) == 1
    assert json.loads(on_disk[0]) == line


def test_marker_requires_recording(obs_factory, tmp_path):
    server = obs_factory(
        requests={**CANVAS_PORTRAIT, "GetRecordStatus": {"outputActive": False, "outputDuration": 0}}
    )
    session_dir = _start(tmp_path, server.port)

    result = _invoke(["--port", str(server.port), "marker", "x", "--session", str(session_dir)])
    assert result.exit_code == 1
    assert "not recording" in result.output


def test_marker_without_session_is_usage_error(obs_factory, tmp_path, monkeypatch):
    server = obs_factory(requests=dict(CANVAS_PORTRAIT))
    monkeypatch.delenv("NAVIG_OBS_SESSION", raising=False)
    result = _invoke(["--port", str(server.port), "marker", "x"])
    assert result.exit_code == 2
    assert "session start" in result.output


def test_marker_bad_kind_is_usage_error(obs_factory, tmp_path):
    server = obs_factory(
        requests={**CANVAS_PORTRAIT, "GetRecordStatus": {"outputActive": True, "outputDuration": 1000}}
    )
    session_dir = _start(tmp_path, server.port)
    result = _invoke(
        ["--port", str(server.port), "marker", "x", "--session", str(session_dir), "--kind", "whatever"]
    )
    assert result.exit_code == 2
    assert "--kind" in result.output


def test_marker_env_session_fallback(obs_factory, tmp_path, monkeypatch):
    server = obs_factory(
        requests={**CANVAS_PORTRAIT, "GetRecordStatus": {"outputActive": True, "outputDuration": 2500}}
    )
    session_dir = _start(tmp_path, server.port)
    monkeypatch.setenv("NAVIG_OBS_SESSION", str(session_dir))
    result = _invoke(["--port", str(server.port), "marker", "env-marker", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["tOffset"] == 2.5


# ── session stop ────────────────────────────────────────────────────────────
def test_stop_closes_record_and_finds_recording(obs_factory, tmp_path):
    rec_dir = tmp_path / "recordings"
    rec_dir.mkdir()
    fresh = rec_dir / "show.mp4"
    fresh.write_bytes(b"\x00")  # mtime = now >= session start

    server = obs_factory(
        requests={
            **CANVAS_PORTRAIT,
            "GetRecordStatus": {"outputActive": False, "outputDuration": 0},
            "GetRecordDirectory": {"recordDirectory": str(rec_dir)},
        }
    )
    session_dir = _start(tmp_path, server.port)

    result = _invoke(["--port", str(server.port), "session", "stop", "--session", str(session_dir), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["endEpoch"] is not None and payload["endEpoch"] >= payload["startEpoch"]
    assert payload["recordingPath"] == str(fresh)


def test_stop_refuses_while_recording(obs_factory, tmp_path):
    server = obs_factory(
        requests={**CANVAS_PORTRAIT, "GetRecordStatus": {"outputActive": True, "outputDuration": 500}}
    )
    session_dir = _start(tmp_path, server.port)
    result = _invoke(["--port", str(server.port), "session", "stop", "--session", str(session_dir)])
    assert result.exit_code == 1
    assert "still recording" in result.output
    # record untouched
    record = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    assert record["endEpoch"] is None
