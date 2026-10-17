"""Spoken lines, the civic watcher, and the raw/refresh/monitor verbs."""

from __future__ import annotations

import asyncio
import json
import subprocess
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import navig_obs
from navig_obs.commands.obs import obs_app
from navig_obs.show import actions, civic
from navig_obs.show import say as say_mod
from navig_obs.show.bridge import ShowBridge
from navig_obs.show.settings import ShowSettings, load_settings

runner = CliRunner()
VENDOR_OK = {
    "CallVendorRequest": {},
    "SetInputSettings": {},
    "TriggerMediaInputAction": {},
}


def test_tests_import_this_checkout():
    # The installed navig-obs is not an editable install of every worktree:
    # prove this run exercises the code next to these tests.
    from pathlib import Path

    here = Path(__file__).resolve().parents[1]
    assert Path(navig_obs.__file__).resolve().is_relative_to(here), navig_obs.__file__


@pytest.fixture
def show_env(tmp_path, monkeypatch):
    """Isolated state dir + settings from env only + a fake TTS/ffprobe."""
    monkeypatch.setattr("navig_obs.show.settings._cfg", lambda _k: None)
    monkeypatch.setattr("navig_obs.show.settings._cfg_raw", lambda _k: None)
    monkeypatch.setattr("navig_obs.show.say.state_dir", lambda: tmp_path)
    monkeypatch.setattr("navig_obs.commands.show.state_dir", lambda: tmp_path)
    for k in (
        "NAVIG_OBS_SHOW_CIVIC_FEED",
        "NAVIG_OBS_SHOW_CIVIC_SAY",
        "NAVIG_OBS_SHOW_SAY_PROVIDER",
        "NAVIG_OBS_SHOW_SAY_VOICE",
        "NAVIG_OBS_SHOW_SAY_VOICES",
        "NAVIG_OBS_SHOW_SAY_LANG",
        "NAVIG_OBS_SHOW_CUE_INPUT",
    ):
        monkeypatch.delenv(k, raising=False)
    calls: list[list[str]] = []
    fake = SimpleNamespace(calls=calls, fail=False)

    def run(argv, **_kw):
        calls.append(list(argv))
        if "voice" in argv:
            if fake.fail:
                return subprocess.CompletedProcess(argv, 3, "", "provider said no")
            out = argv[argv.index("--output") + 1]
            with open(out, "wb") as fh:
                fh.write(b"ID3fake")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "2.5\n", "")  # ffprobe

    monkeypatch.setattr("navig_obs.show.say.subprocess.run", run)
    monkeypatch.setattr("navig_obs.show.say.shutil.which", lambda name: name)
    fake.dir = tmp_path
    return fake


def _synth_calls(fake) -> list[list[str]]:
    return [c for c in fake.calls if "voice" in c]


STAGE_READ = "GetSceneCollectionList"


def _moves(received) -> list:
    """What the show DID to OBS (the stage-language read is only a look)."""
    return [r for r in received if r[0] != STAGE_READ]


# ── say ──────────────────────────────────────────────────────────────────────
def test_say_sends_card_then_pigeon_then_clip(obs_factory, show_env):
    server = obs_factory(requests=VENDOR_OK)
    result = runner.invoke(
        obs_app,
        [
            "--port",
            str(server.port),
            "show",
            "say",
            "SAINT PETERSBURG has ignited.",
            "--title",
            "Ignition",
            "--kicker",
            "THE GRID",
            "--tone",
            "game",
            "--voice",
            "v1",
        ],
    )
    assert result.exit_code == 0, result.output
    synth = _synth_calls(show_env)
    assert len(synth) == 1
    argv = synth[0]
    assert argv[:4] == ["navig", "voice", "speak", "SAINT PETERSBURG has ignited."]
    # No stage language (the fake OBS names none) → English, whose voice is
    # MiMo; --voice swaps the voice id and keeps the language's provider.
    assert argv[argv.index("--provider") + 1] == "mimo"
    assert argv[argv.index("--voice") + 1] == "v1"
    assert argv[argv.index("--lang") + 1] == "en"
    assert argv[-1] == "--no-play"
    moves = _moves(server.received)
    clip = moves[2][1]["inputSettings"]["local_file"]
    from pathlib import Path

    assert Path(clip).parent == show_env.dir / "tts" and Path(clip).is_file()

    types = [r[0] for r in moves]
    assert types == [
        "CallVendorRequest",
        "CallVendorRequest",
        "SetInputSettings",
        "TriggerMediaInputAction",
    ]
    toast, pigeon, settings, trigger = (r[1] for r in moves)
    assert toast["requestData"] == {
        "event_name": "schema-toast",
        "event_data": {
            "kind": "alert",
            "tone": "game",
            "kicker": "THE GRID",
            "title": "Ignition",
            "text": "SAINT PETERSBURG has ignited.",
            "ms": 2500 + say_mod.CARD_TAIL_MS,
        },
    }
    assert pigeon["requestData"] == {
        "event_name": "pigeon",
        "event_data": {
            "act": "speak",
            "ms": 2500,
            "text": "SAINT PETERSBURG has ignited.",
        },
    }
    assert settings == {
        "inputName": "SCHEMA · Cue",
        "inputSettings": {"is_local_file": True, "local_file": clip},
        "overlay": True,
    }
    assert trigger == {
        "inputName": "SCHEMA · Cue",
        "mediaAction": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART",
    }


def test_say_title_defaults_to_text_and_body_empty(show_env):
    p = say_mod.plan("Hello Grid", tone="info")
    assert p.toast()["title"] == "Hello Grid" and p.toast()["text"] == ""


def test_say_cache_hit_never_resynthesizes(obs_factory, show_env):
    server = obs_factory(requests=VENDOR_OK)
    args = ["--port", str(server.port), "show", "say", "Twice the same line", "--json"]
    assert runner.invoke(obs_app, args).exit_code == 0
    assert len(_synth_calls(show_env)) == 1
    second = runner.invoke(obs_app, args)
    assert second.exit_code == 0, second.output
    assert len(_synth_calls(show_env)) == 1  # cache hit: no new synthesis
    assert json.loads(second.output)["cached"] is True


def test_say_dry_run_touches_nothing(unused_port, show_env):
    result = runner.invoke(
        obs_app,
        [
            "--port",
            str(unused_port),
            "show",
            "say",
            "Nothing happens",
            "--dry-run",
            "--json",
        ],
    )
    assert result.exit_code == 0, (
        result.output
    )  # no OBS reachable, yet exit 0: no call made
    assert show_env.calls == []
    plan = json.loads(result.output)
    assert plan["dryRun"] is True and plan["cached"] is False
    assert plan["pigeon"]["act"] == "speak"
    assert plan["pigeon"]["ms"] == say_mod.MIN_MS
    assert plan["pigeon"]["text"] == plan["text"]  # the bubble shows the spoken line


def test_say_dry_run_makes_no_obs_request(obs_factory, show_env):
    server = obs_factory(requests=VENDOR_OK)
    result = runner.invoke(
        obs_app, ["--port", str(server.port), "show", "say", "x", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert server.received == [] and show_env.calls == []


def test_say_synthesis_failure_still_shows_the_card(obs_factory, show_env):
    show_env.fail = True
    server = obs_factory(requests=VENDOR_OK)
    result = runner.invoke(
        obs_app,
        [
            "--port",
            str(server.port),
            "show",
            "say",
            "one two three",
            "--provider",
            "mimo",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "provider said no" in result.output
    assert [r[0] for r in _moves(server.received)] == [
        "CallVendorRequest",
        "CallVendorRequest",
    ]  # no clip
    assert not list((show_env.dir / "tts").glob("*.mp3"))  # nothing half-written cached
    assert _synth_calls(show_env)[0][5] == "mimo"  # provider is a passthrough


def test_say_no_voice_skips_synthesis(obs_factory, show_env):
    server = obs_factory(requests=VENDOR_OK)
    result = runner.invoke(
        obs_app, ["--port", str(server.port), "show", "say", "quiet", "--no-voice"]
    )
    assert result.exit_code == 0, result.output
    assert show_env.calls == []
    assert [r[0] for r in _moves(server.received)] == [
        "CallVendorRequest",
        "CallVendorRequest",
    ]


def test_say_bad_tone_is_usage_error(show_env):
    result = runner.invoke(obs_app, ["show", "say", "x", "--tone", "loud", "--dry-run"])
    assert result.exit_code == 2


def test_estimate_floor_and_words():
    assert say_mod.estimate_ms("a b") == say_mod.MIN_MS
    assert say_mod.estimate_ms(" ".join(["w"] * 20)) == 20 * say_mod.MS_PER_WORD


# ── civic: the spoken line (obs.show.civic_say) on top of the civic watcher ──
def _civic_doc(*rows: tuple[str, str, str | None]) -> dict:
    """rows = (occurredAt, kind, city) — newest first like the real feed."""
    return {
        "data": [
            {
                "id": at,
                "kind": kind,
                "zone": {"slug": c.lower(), "name": c, "country": "RU"} if c else None,
                "occurredAt": at,
                "payload": {},
            }
            for at, kind, c in sorted(rows, reverse=True)
        ],
        "serverTime": "2026-10-15T12:00:00Z",
    }


T1, T2 = "2026-10-15T10:00:00Z", "2026-10-15T10:05:00Z"


def test_civic_line_is_world_voiced():
    ev = _civic_doc((T1, "ignition", "Saint Petersburg"))["data"][0]
    assert civic.city(ev) == "Saint Petersburg"
    assert civic.line(ev) == "SAINT PETERSBURG has ignited."
    bare = _civic_doc((T1, "broadcast_live", None))["data"][0]
    assert civic.line(bare) == "The broadcast is live."
    assert civic.line(_civic_doc((T1, "ignition", None))["data"][0]) is None
    assert civic.line({"kind": "unknown_kind"}) is None


class Recorder:
    def __init__(self):
        self.calls: list[tuple[str, dict | None]] = []

    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    def request(self, rtype: str, data: dict | None = None) -> dict:
        self.calls.append((rtype, data))
        return {}


def _civic_bridge(tmp_path, rec, docs, *, say: bool):
    it = iter(docs)
    prepared: list[str] = []

    def prep(p):
        prepared.append(p.text)
        p.ms = 1234
        p.clip = None  # as if synthesis failed: card + pigeon only
        return p

    b = ShowBridge(
        ShowSettings(
            enabled=True,
            patch_feed="",
            civic_feed="https://civic.test",
            civic_say=say,
            say_on_air_only=False,  # the on-air guard has its own tests
            cues_file=tmp_path / "none.json",
        ),
        client_factory=lambda: rec,
        fetcher=lambda _url: next(it),
        state_path=tmp_path / "st.json",
        voice_prepare=prep,
    )
    return b, prepared


def test_bridge_civic_stays_silent_by_default(tmp_path):
    rec = Recorder()
    b, prepared = _civic_bridge(
        tmp_path,
        rec,
        [
            _civic_doc((T1, "zone_founded", "Kazan")),
            _civic_doc((T2, "ignition", "Kazan")),
        ],
        say=False,
    )
    asyncio.run(b.poll_civic_once())
    asyncio.run(b.poll_civic_once())
    assert [c[1]["requestData"]["event_name"] for c in rec.calls] == ["pigeonCivic"]
    assert prepared == []  # civic_say off: no TTS, ever


def test_bridge_civic_say_speaks_the_line(tmp_path):
    rec = Recorder()
    b, prepared = _civic_bridge(
        tmp_path,
        rec,
        [
            _civic_doc((T1, "ignition", "Kazan")),
            _civic_doc((T2, "ignition", "Saint Petersburg")),
        ],
        say=True,
    )
    asyncio.run(b.poll_civic_once())  # seed: nothing said
    asyncio.run(b.poll_civic_once())
    assert prepared == ["SAINT PETERSBURG has ignited."]
    calls = _moves(rec.calls)
    names = [c[1]["requestData"]["event_name"] for c in calls]
    assert names == ["pigeonCivic", "schema-toast", "pigeon"]
    toast = calls[1][1]["requestData"]["event_data"]
    assert toast["kind"] == "alert" and toast["kicker"] == "THE GRID"
    assert toast["tone"] == "live"  # an ignition is a live beat
    assert calls[2][1]["requestData"]["event_data"] == {
        "act": "speak",
        "ms": 1234,
        "text": "SAINT PETERSBURG has ignited.",
    }


def test_cli_civic_fire_speaks_only_with_civic_say(obs_factory, show_env, monkeypatch):
    monkeypatch.setenv("NAVIG_OBS_SHOW_CIVIC_SAY", "1")
    docs = iter(
        [
            _civic_doc((T1, "zone_founded", "Kazan")),
            _civic_doc((T2, "mayor_elected", "Kazan")),
        ]
    )
    monkeypatch.setattr("navig_obs.commands.show.feed.fetch", lambda _u: next(docs))
    server = obs_factory(requests=VENDOR_OK)
    base = ["--port", str(server.port), "show", "civic", "--fire", "--json"]
    assert runner.invoke(obs_app, base).exit_code == 0  # seed
    assert server.received == [] and show_env.calls == []
    fired = runner.invoke(obs_app, base)
    assert fired.exit_code == 0, fired.output
    synth = _synth_calls(show_env)
    assert len(synth) == 1 and synth[0][3] == "KAZAN has a new mayor."
    names = [
        r[1]["requestData"]["event_name"]
        for r in server.received
        if r[0] == "CallVendorRequest"
    ]
    assert names == ["pigeonCivic", "schema-toast", "pigeon"]
    assert [r[0] for r in server.received][-2:] == [
        "SetInputSettings",
        "TriggerMediaInputAction",
    ]


def test_say_settings_defaults(monkeypatch):
    monkeypatch.setattr("navig_obs.show.settings._cfg", lambda _k: None)
    for k in (
        "NAVIG_OBS_SHOW_CIVIC_SAY",
        "NAVIG_OBS_SHOW_SAY_PROVIDER",
        "NAVIG_OBS_SHOW_SAY_VOICE",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("navig_obs.show.settings._cfg_raw", lambda _k: None)
    s = load_settings()
    # No forced provider: each language's voice decides (voices.DEFAULTS).
    assert s.civic_say is False and s.say_provider is None and s.say_voice is None
    assert s.say_lang == "en"
    monkeypatch.setenv("NAVIG_OBS_SHOW_SAY_PROVIDER", "mimo")
    monkeypatch.setenv("NAVIG_OBS_SHOW_SAY_VOICE", "nova")
    s = load_settings()
    assert s.say_provider == "mimo" and s.say_voice == "nova"


# ── raw request · refresh · audio monitor ────────────────────────────────────
def test_request_passthrough(obs_factory):
    server = obs_factory(
        requests={
            "GetInputSettings": lambda d: {"inputSettings": {"url": "x"}, "echo": d}
        }
    )
    result = runner.invoke(
        obs_app,
        [
            "--port",
            str(server.port),
            "request",
            "GetInputSettings",
            "--data",
            '{"inputName":"Grid Pigeon"}',
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert server.received == [("GetInputSettings", {"inputName": "Grid Pigeon"})]
    assert json.loads(result.output)["inputSettings"] == {"url": "x"}


def test_request_without_data_sends_none(obs_factory):
    server = obs_factory(requests={"GetVersion": {"obsVersion": "32.2.2"}})
    result = runner.invoke(
        obs_app, ["--port", str(server.port), "request", "GetVersion"]
    )
    assert result.exit_code == 0, result.output
    assert server.received == [("GetVersion", None)]


@pytest.mark.parametrize("bad", ["{nope", "[1,2]"])
def test_request_bad_json_is_usage_error(bad, obs_factory):
    server = obs_factory()
    result = runner.invoke(
        obs_app, ["--port", str(server.port), "request", "GetVersion", "--data", bad]
    )
    assert result.exit_code == 2
    assert server.received == []


def test_request_rejected_exits_one(obs_factory):
    server = obs_factory()
    result = runner.invoke(
        obs_app, ["--port", str(server.port), "request", "StartStreamz"]
    )
    assert result.exit_code == 1


def test_sources_refresh_presses_refreshnocache(obs_factory):
    server = obs_factory(requests={"PressInputPropertiesButton": {}})
    result = runner.invoke(
        obs_app, ["--port", str(server.port), "sources", "refresh", "Grid Pigeon"]
    )
    assert result.exit_code == 0, result.output
    assert server.received == [
        (
            "PressInputPropertiesButton",
            {"inputName": "Grid Pigeon", "propertyName": "refreshnocache"},
        )
    ]


@pytest.mark.parametrize(
    "flag,wire",
    [
        ("none", "OBS_MONITORING_TYPE_NONE"),
        ("monitor", "OBS_MONITORING_TYPE_MONITOR_ONLY"),
        ("both", "OBS_MONITORING_TYPE_MONITOR_AND_OUTPUT"),
    ],
)
def test_audio_monitor_set(flag, wire, obs_factory):
    server = obs_factory(requests={"SetInputAudioMonitorType": {}})
    result = runner.invoke(
        obs_app,
        [
            "--port",
            str(server.port),
            "audio",
            "monitor",
            "SCHEMA · Cue",
            "--type",
            flag,
        ],
    )
    assert result.exit_code == 0, result.output
    assert server.received == [
        ("SetInputAudioMonitorType", {"inputName": "SCHEMA · Cue", "monitorType": wire})
    ]


def test_audio_monitor_read(obs_factory):
    server = obs_factory(
        requests={
            "GetInputAudioMonitorType": {
                "monitorType": "OBS_MONITORING_TYPE_MONITOR_AND_OUTPUT"
            }
        }
    )
    result = runner.invoke(
        obs_app,
        ["--port", str(server.port), "audio", "monitor", "SCHEMA · Cue", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert server.received == [
        ("GetInputAudioMonitorType", {"inputName": "SCHEMA · Cue"})
    ]
    assert json.loads(result.output)["type"] == "both"


def test_audio_monitor_bad_type_is_usage_error(obs_factory):
    server = obs_factory()
    result = runner.invoke(
        obs_app, ["--port", str(server.port), "audio", "monitor", "X", "--type", "loud"]
    )
    assert result.exit_code == 2
    assert server.received == []


def test_emit_event_is_the_toast_path():
    rec = Recorder()
    actions.emit_event(rec, "pigeon", {"act": "speak", "ms": 1})
    actions.emit_toast(rec, "schema-toast", {"kind": "alert"})
    assert [c[1]["requestData"]["event_name"] for c in rec.calls] == [
        "pigeon",
        "schema-toast",
    ]
