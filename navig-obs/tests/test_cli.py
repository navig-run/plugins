"""CLI tests: exit honesty, masking, rendering — through the real Typer app."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from conftest import TINY_PNG
from navig_obs.commands.obs import obs_app

runner = CliRunner()


def _invoke(args: list[str]):
    return runner.invoke(obs_app, args)


# ── status: unreachable is a SKIP (exit 0), actions unreachable are FAIL ─────
def test_status_unreachable_is_skip_exit_zero(unused_port):
    result = _invoke(["--port", str(unused_port), "status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["reachable"] is False


def test_status_connected_renders(obs_factory):
    server = obs_factory(
        requests={
            "GetVersion": {"obsVersion": "32.2.2", "obsWebSocketVersion": "5.5.2", "rpcVersion": 1},
            "GetStreamStatus": {"outputActive": False},
            "GetRecordStatus": {"outputActive": True, "outputDuration": 65000},
            "GetCurrentProgramScene": {"currentProgramSceneName": "SCHEMA LIVE"},
            "GetStudioModeEnabled": {"studioModeEnabled": False},
            "GetReplayBufferStatus": {"outputActive": False},
            "GetVirtualCamStatus": {"outputActive": False},
        }
    )
    result = _invoke(["--port", str(server.port), "status"])
    assert result.exit_code == 0, result.output
    assert "32.2.2" in result.output
    assert "SCHEMA LIVE" in result.output
    assert "record" in result.output  # the record row exists
    assert "● on" in result.output  # record outputActive=True renders as on


def test_action_unreachable_exits_one(unused_port):
    result = _invoke(["--port", str(unused_port), "stream", "start"])
    assert result.exit_code == 1
    assert "cannot reach OBS" in result.output or "✗" in result.output


def test_request_rejection_exits_one(obs_factory):
    server = obs_factory()  # no handlers → code 600 for everything
    result = _invoke(["--port", str(server.port), "stream", "start"])
    assert result.exit_code == 1
    assert "Unknown request type StartStream" in result.output


# ── scenes ───────────────────────────────────────────────────────────────────
def test_scenes_list_marks_current(obs_factory):
    server = obs_factory(
        requests={
            "GetSceneList": {
                "currentProgramSceneName": "BRB",
                "currentPreviewSceneName": None,
                "scenes": [{"sceneName": "LIVE"}, {"sceneName": "BRB"}],
            }
        }
    )
    result = _invoke(["--port", str(server.port), "scenes", "list"])
    assert result.exit_code == 0, result.output
    assert "LIVE" in result.output and "BRB" in result.output
    assert "program" in result.output


def test_scenes_activate(obs_factory):
    server = obs_factory(requests={"SetCurrentProgramScene": {}})
    result = _invoke(["--port", str(server.port), "scenes", "activate", "Emergency Cut"])
    assert result.exit_code == 0, result.output
    assert ("SetCurrentProgramScene", {"sceneName": "Emergency Cut"}) in server.received


def test_scenes_create(obs_factory):
    server = obs_factory(requests={"CreateScene": {}})
    result = _invoke(["--port", str(server.port), "scenes", "create", "Disorder Protocol"])
    assert result.exit_code == 0
    assert ("CreateScene", {"sceneName": "Disorder Protocol"}) in server.received


# ── stream settings: the key must be masked by default ───────────────────────
def test_stream_settings_masks_key(obs_factory):
    server = obs_factory(
        requests={
            "GetStreamSettings": {
                "streamServiceType": "rtmp_custom",
                "streamSettings": {"server": "rtmp://live.example/app", "key": "SUPERSECRETKEY123"},
            }
        }
    )
    result = _invoke(["--port", str(server.port), "stream", "settings"])
    assert result.exit_code == 0, result.output
    assert "SUPERSECRETKEY123" not in result.output
    assert "********" in result.output


def test_stream_settings_reveal_shows_key(obs_factory):
    server = obs_factory(
        requests={
            "GetStreamSettings": {
                "streamServiceType": "rtmp_custom",
                "streamSettings": {"server": "rtmp://live.example/app", "key": "SUPERSECRETKEY123"},
            }
        }
    )
    result = _invoke(["--port", str(server.port), "stream", "settings", "--reveal", "--json"])
    assert result.exit_code == 0
    assert "SUPERSECRETKEY123" in result.output


def test_stream_settings_write_merges(obs_factory):
    server = obs_factory(
        requests={
            "GetStreamSettings": {
                "streamServiceType": "rtmp_custom",
                "streamSettings": {"server": "rtmp://old", "key": "oldkey"},
            },
            "SetStreamSettings": {},
        }
    )
    result = _invoke(
        ["--port", str(server.port), "stream", "settings", "--set-key", "newkey", "--set-server", "rtmp://new"]
    )
    assert result.exit_code == 0, result.output
    set_calls = [data for rt, data in server.received if rt == "SetStreamSettings"]
    assert len(set_calls) == 1
    assert set_calls[0]["streamSettings"]["key"] == "newkey"
    assert set_calls[0]["streamSettings"]["server"] == "rtmp://new"
    # the new key must NOT be echoed in the success line
    assert "newkey" not in result.output


# ── screenshot ───────────────────────────────────────────────────────────────
def test_screenshot_writes_file(obs_factory, tmp_path):
    # OBS writes the file itself at imageFilePath (protocol requirement) —
    # the fake server simulates that write.
    from pathlib import Path as _Path

    def _fake_save(request_data):
        _Path(request_data["imageFilePath"]).write_bytes(TINY_PNG)
        return {}

    server = obs_factory(requests={"SaveSourceScreenshot": _fake_save})
    out = tmp_path / "proof.png"
    result = _invoke(["--port", str(server.port), "screenshot", "Program", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists() and out.read_bytes() == TINY_PNG
    # the protocol fields must be present — OBS 32 rejects the request without them
    sent = next(data for rt, data in server.received if rt == "SaveSourceScreenshot")
    assert sent["imageFilePath"] == str(out)
    assert sent["imageFormat"] == "png"


def test_screenshot_bad_format_is_usage_error(obs_factory):
    server = obs_factory(requests={"SaveSourceScreenshot": {}})
    result = _invoke(["--port", str(server.port), "screenshot", "Program", "--out", "x.gif"])
    assert result.exit_code == 2


# ── events ───────────────────────────────────────────────────────────────────
def test_events_unknown_topic_is_usage_error(obs_factory):
    server = obs_factory()
    result = _invoke(["--port", str(server.port), "events", "--topics", "nonsense"])
    assert result.exit_code == 2
    assert "unknown topic" in result.output


def test_events_streams_jsonl(obs_factory):
    server = obs_factory(
        events=[
            {
                "eventType": "CurrentProgramSceneChanged",
                "eventIntent": 4,
                "eventData": {"sceneName": "BRB"},
            }
        ]
    )
    result = _invoke(["--port", str(server.port), "events", "--topics", "scenes", "--timeout", "5", "--json"])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip().startswith("{")]
    assert lines, result.output
    payload = json.loads(lines[0])
    assert payload["eventType"] == "CurrentProgramSceneChanged"
    assert server.subscriptions == 1 << 2  # EVENT_SCENES


# ── items / sources happy paths ──────────────────────────────────────────────
def test_items_list_uses_current_scene(obs_factory):
    server = obs_factory(
        requests={
            "GetCurrentProgramScene": {"currentProgramSceneName": "LIVE"},
            "GetSceneItemList": {
                "sceneItems": [
                    {
                        "sceneItemId": 3,
                        "inputName": "webcam",
                        "inputKind": "dshow_input",
                        "sceneItemEnabled": True,
                        "sceneItemLocked": False,
                    }
                ]
            },
        }
    )
    result = _invoke(["--port", str(server.port), "items", "list"])
    assert result.exit_code == 0, result.output
    assert "webcam" in result.output and "visible" in result.output


def test_sources_settings_set(obs_factory):
    server = obs_factory(requests={"SetInputSettings": {}})
    result = _invoke(
        ["--port", str(server.port), "sources", "settings", "overlay", "--set", '{"url":"http://x/?e=1"}']
    )
    assert result.exit_code == 0, result.output
    assert ("SetInputSettings", {"inputName": "overlay", "inputSettings": {"url": "http://x/?e=1"}}) in (
        server.received
    )


# ── media: v5 has ONE transport request, TriggerMediaInputAction ─────────────
# Only TriggerMediaInputAction is scripted, exactly as a real OBS 5.x answers:
# the old per-action names (PlayMediaInput, …) are not v5 requests, so the
# previous code fails here the way it failed against a real OBS (204).
import pytest  # noqa: E402


@pytest.mark.parametrize(
    ("verb", "action"),
    [
        ("play", "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY"),
        ("pause", "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PAUSE"),
        ("restart", "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART"),
        ("stop", "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_STOP"),
    ],
)
def test_media_verbs_use_the_v5_trigger_request(obs_factory, verb, action):
    server = obs_factory(requests={"TriggerMediaInputAction": {}})
    result = _invoke(["--port", str(server.port), "media", verb, "--source", "Event Sound"])
    assert result.exit_code == 0, result.output
    assert (
        "TriggerMediaInputAction",
        {"inputName": "Event Sound", "mediaAction": action},
    ) in server.received
    sent = {rtype for rtype, _ in server.received}
    assert not sent & {"PlayMediaInput", "PauseMediaInput", "RestartMediaInput", "StopMediaInput"}
