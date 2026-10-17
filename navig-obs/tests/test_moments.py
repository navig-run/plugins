"""Pigeon moments → markers → vertical clips."""

from __future__ import annotations

import asyncio
import json
import os
import time

from typer.testing import CliRunner

from navig_obs.commands.obs import obs_app
from navig_obs.plugin import _moment_request_allowed
from navig_obs.show import clips, moments
from navig_obs.show.bridge import ShowBridge
from navig_obs.show.settings import ShowSettings

runner = CliRunner()
TOKEN = "t" * 40


# ── what the page may report ─────────────────────────────────────────────────
def test_validate_accepts_a_headline_and_a_line():
    assert moments.validate({"token": TOKEN, "kind": "hacker", "lang": "fr"}, TOKEN) == {
        "kind": "hacker", "lang": "fr"}
    m = moments.validate(
        {"token": TOKEN, "kind": "line", "group": "fury", "text": "  Easy.\nThe keyboard ", "ms": 2400},
        TOKEN,
    )
    assert m == {"kind": "line", "lang": "en", "text": "Easy. The keyboard", "group": "fury", "ms": 2400}


def test_validate_refuses_by_name():
    import pytest

    for payload, why in [
        ({"token": "nope", "kind": "hacker"}, "bad token"),
        ({"token": TOKEN, "kind": "rm -rf"}, "unknown kind"),
        ({"token": TOKEN, "kind": "line"}, "needs its text"),
        ("not a dict", "not an object"),
    ]:
        with pytest.raises(moments.MomentRejected, match=why):
            moments.validate(payload, TOKEN)
    # an empty server token refuses everything, even an empty claim
    with pytest.raises(moments.MomentRejected):
        moments.validate({"token": "", "kind": "hacker"}, "")


def test_validate_caps_text_and_cleans_group():
    m = moments.validate({"token": TOKEN, "kind": "line", "text": "x" * 999, "group": "../evil", "ms": 10**9}, TOKEN)
    assert len(m["text"]) == moments.MAX_TEXT and m["group"] == "line" and m["ms"] == 60000


def test_rate_limit():
    r = moments.RateLimit(per_minute=3)
    assert [r.allow(0), r.allow(1), r.allow(2), r.allow(3)] == [True, True, True, False]
    assert r.allow(61)


def test_token_is_minted_once(tmp_path):
    p = tmp_path / "tok"
    a = moments.ensure_token(p)
    assert len(a) >= 32 and moments.ensure_token(p) == a


# ── the route guard ──────────────────────────────────────────────────────────
def test_route_takes_only_the_local_obs_page():
    ok = {"Origin": "http://absolute"}
    assert _moment_request_allowed("127.0.0.1", "http://absolute", ok)
    assert not _moment_request_allowed("10.0.0.5", "http://absolute", ok)
    assert not _moment_request_allowed("127.0.0.1", "https://evil.example", ok)
    assert not _moment_request_allowed("127.0.0.1", None, {})
    # loopback by socket, but tunnelled in: refused
    assert not _moment_request_allowed("127.0.0.1", "http://absolute", {**ok, "CF-Connecting-IP": "1.2.3.4"})
    assert not _moment_request_allowed("::1", "http://absolute", {**ok, "X-Forwarded-For": "1.2.3.4"})


# ── the recorder ─────────────────────────────────────────────────────────────
CTX = {"scene": "02 The Architect", "collection": "SCHEMA BROADCAST (fr)", "portrait": False,
       "recordDirectory": "D:/rec"}


def test_first_moment_opens_a_contract_session_and_later_ones_reuse_it(tmp_path):
    r = moments.Recorder(root=tmp_path)
    now = 1_760_000_000_000
    a = r.record({"kind": "hacker", "lang": "fr"}, {"outputActive": True, "outputDuration": 12_000}, CTX, now)
    b = r.record({"kind": "lag", "lang": "fr"}, {"outputActive": True, "outputDuration": 42_000}, CTX, now + 30_000)
    assert a["session"] == b["session"]
    d = tmp_path / os.path.basename(a["session"])
    rec = json.loads((d / "session.json").read_text(encoding="utf-8"))
    assert rec["contractVersion"] == 1 and rec["startEpoch"] == now - 12_000
    assert rec["lang"] == "fr" and rec["recordingPath"] is None and rec["endEpoch"] is None
    assert set(rec) == {"contractVersion", "sessionId", "startEpoch", "scene", "collection", "lang",
                        "portrait", "recordingPath", "markersFile", "endEpoch", "showId"}
    rows = clips.read_jsonl(d / "markers.jsonl")
    assert [m["label"] for m in rows] == ["pigeon-hacker", "pigeon-lag"]
    assert [m["tOffset"] for m in rows] == [12.0, 42.0]
    # contract 7 marker: exactly these fields, nothing extra
    assert all(set(m) == {"contractVersion", "tOffset", "wallClock", "label", "kind"} for m in rows)
    assert json.loads((d / moments.SIDE_META).read_text(encoding="utf-8"))["recordDirectory"] == "D:/rec"


def test_a_new_recording_gets_a_new_session(tmp_path):
    r = moments.Recorder(root=tmp_path)
    now = 1_760_000_000_000
    a = r.record({"kind": "hacker", "lang": "en"}, {"outputActive": True, "outputDuration": 5_000}, CTX, now)
    b = r.record({"kind": "hacker", "lang": "en"}, {"outputActive": True, "outputDuration": 5_000}, CTX, now + 600_000)
    assert a["session"] != b["session"]


def test_lines_go_to_the_sidecar_not_the_contract_file(tmp_path):
    r = moments.Recorder(root=tmp_path)
    out = r.record({"kind": "line", "group": "fury", "text": "Easy.", "ms": 2000, "lang": "en"},
                   {"outputActive": True, "outputDuration": 3_000}, CTX, 1_760_000_000_000)
    d = tmp_path / os.path.basename(out["session"])
    assert clips.read_jsonl(d / "markers.jsonl") == []
    assert clips.read_jsonl(d / moments.SIDE_LINES)[0]["text"] == "Easy."


def test_no_recording_no_marker(tmp_path):
    r = moments.Recorder(root=tmp_path)
    assert r.record({"kind": "hacker", "lang": "en"}, {"outputActive": False}, CTX) is None
    assert list(tmp_path.iterdir()) == []


# ── the bridge path ──────────────────────────────────────────────────────────
class RecOBS:
    def __init__(self, active=True):
        self.active = active
        self.calls = []

    def connect(self):
        pass

    def close(self):
        pass

    def request(self, rtype, data=None):
        self.calls.append(rtype)
        if rtype == "GetRecordStatus":
            return {"outputActive": self.active, "outputDuration": 7_000}
        if rtype == "GetSceneCollectionList":
            return {"currentSceneCollectionName": "SCHEMA BROADCAST (ru)"}
        if rtype == "GetVideoInfo":
            return {"baseCX": 1920, "baseCY": 1080}
        return {}


def _bridge(tmp_path, obs):
    b = ShowBridge(ShowSettings(enabled=True, patch_feed="", civic_feed=""), client_factory=lambda: obs)
    b._moment_token = TOKEN
    b._moments = moments.Recorder(root=tmp_path)
    return b


def test_bridge_records_a_moment_while_recording(tmp_path):
    obs = RecOBS()
    b = _bridge(tmp_path, obs)
    out = asyncio.run(b.moment({"token": TOKEN, "kind": "celebrate", "lang": "ru"}))
    assert out["ok"] and out["marker"]["label"] == "pigeon-celebrate"
    rec = json.loads((tmp_path / os.path.basename(out["session"]) / "session.json").read_text(encoding="utf-8"))
    assert rec["lang"] == "ru"
    # a second moment reuses the held connection and the session (no context asked again)
    asyncio.run(b.moment({"token": TOKEN, "kind": "lag", "lang": "ru"}))
    assert obs.calls.count("GetSceneCollectionList") == 1


def test_bridge_refuses_bad_reports_and_skips_when_not_recording(tmp_path):
    b = _bridge(tmp_path, RecOBS(active=False))
    assert asyncio.run(b.moment({"token": "x", "kind": "hacker"})) == {"ok": False, "why": "bad token"}
    assert asyncio.run(b.moment({"token": TOKEN, "kind": "hacker"}))["why"] == "not recording"


# ── planning clips ───────────────────────────────────────────────────────────
def _m(t, kind):
    return {"contractVersion": 1, "tOffset": t, "wallClock": 0, "label": f"pigeon-{kind}", "kind": "character"}


def test_plan_windows_merge_and_captions():
    markers = [_m(10, "hacker"), _m(21, "lag"), _m(100, "celebrate"), {"label": "manual", "tOffset": 50}]
    lines = [{"tOffset": 16.5, "text": "Easy. The keyboard has a family.", "ms": 3000},
             {"tOffset": 300, "text": "far away", "ms": 2000}]
    p = clips.plan(markers, lines, duration=200)
    assert [(c.t0, c.t1, c.kinds) for c in p] == [(8.5, 30.0, ["hacker", "lag"]), (98.5, 106.0, ["celebrate"])]
    assert p[0].captions[0].t0 == 8.0 and p[0].captions[0].text.startswith("Easy.")
    assert p[1].captions == []


def test_plan_respects_the_recording_end_and_the_max_length():
    p = clips.plan([_m(0.5, "hacker")], [], duration=5)
    assert p[0].t0 == 0.0 and p[0].t1 == 5
    many = clips.plan([_m(t, "hacker") for t in range(0, 120, 5)], [])
    assert all(c.t1 - c.t0 <= clips.MAX_CLIP_S + 13 for c in many)


def test_crop_follows_the_pigeon_and_never_leaves_the_frame():
    tall = clips.crop_filter(1920, 1080, 0.82, "tall")
    assert tall.startswith("[0:v]crop=608:1080:1270:0,scale=1080:1920") and tall.endswith("[v]")
    assert clips.crop_filter(1920, 1080, 1.0, "tall").startswith("[0:v]crop=608:1080:1312:0")
    assert clips.crop_filter(1920, 1080, 0.0, "tall").startswith("[0:v]crop=608:1080:0:0")
    # square (default): a 1080 square clamped inside the frame, over its blur
    sq = clips.crop_filter(1920, 1080, 0.82)
    # 864 square (80 % of 1080), x centred on him and clamped, bottom just under his feet
    assert sq.startswith("[0:v]crop=864:864:1056:118,split[a][b]")
    assert "boxblur" in sq and "overlay=0:192" in sq and sq.endswith("[v]")
    # never outside the frame, whatever the knobs say
    assert clips.crop_filter(1920, 1080, 0.82, feet=1.5).startswith("[0:v]crop=864:864:1056:216")
    assert clips.crop_filter(1920, 1080, 0.82, feet=0.0).startswith("[0:v]crop=864:864:1056:0")
    # 4K canvas scales with it
    assert clips.crop_filter(3840, 2160, 0.82).startswith("[0:v]crop=1728:1728:2112:238")
    assert clips.crop_filter(1080, 1920, 0.82).startswith("[0:v]scale=1080:1920:force_original_aspect_ratio")


def test_srt_shape():
    out = clips.srt([clips.Caption(1.25, 3.5, "Hello")])
    assert out == "1\n00:00:01,250 --> 00:00:03,500\nHello\n"


def test_find_recording_picks_the_file_created_at_the_start(tmp_path):
    start = time.time() - 100
    old = tmp_path / "old.mkv"
    old.write_bytes(b"x")
    os.utime(old, (start - 3600, start - 3600))
    mine = tmp_path / "mine.mkv"
    mine.write_bytes(b"x")
    rec = {"startEpoch": int(time.time() * 1000)}
    assert clips.find_recording(tmp_path, rec, {"recordDirectory": str(tmp_path)}) == mine
    assert clips.find_recording(tmp_path, rec, {}) is None


# ── the command ──────────────────────────────────────────────────────────────
def test_cli_dry_run_prints_the_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVIG_OBS_SESSIONS", str(tmp_path))
    r = moments.Recorder(root=tmp_path)
    now = int(time.time() * 1000)
    r.record({"kind": "hacker", "lang": "en"}, {"outputActive": True, "outputDuration": 20_000}, CTX, now)
    r.record({"kind": "line", "group": "fury", "text": "Easy.", "ms": 2000, "lang": "en"},
             {"outputActive": True, "outputDuration": 26_000}, CTX, now + 6_000)
    res = runner.invoke(obs_app, ["clips", "--dry-run", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["moments"] == 1 and data["lines"] == 1
    assert data["clips"][0]["kinds"] == ["hacker"] and data["clips"][0]["captions"][0]["text"] == "Easy."


def test_cli_without_sessions_says_what_to_do(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVIG_OBS_SESSIONS", str(tmp_path))
    res = runner.invoke(obs_app, ["clips"])
    assert res.exit_code == 2 and "record with the pigeon" in res.output


# ── a start is confirmed before it is announced ──────────────────────────────
def test_record_start_fails_when_obs_never_starts(obs_factory, monkeypatch):
    # 2026-10-16: OBS answered StartRecord 200, then logged an encoder error
    # and recorded nothing — while navig printed "recording started".
    from navig_obs.commands import output

    monkeypatch.setattr(output, "START_CONFIRM_S", 0.3)
    monkeypatch.setattr(output, "START_POLL_S", 0.05)
    server = obs_factory(requests={"StartRecord": {}, "GetRecordStatus": {"outputActive": False}})
    res = runner.invoke(obs_app, ["--port", str(server.port), "record", "start"])
    assert res.exit_code != 0
    assert "not recording" in res.output and "recording started" not in res.output


def test_record_start_ok_when_obs_is_recording(obs_factory):
    server = obs_factory(requests={"StartRecord": {}, "GetRecordStatus": {"outputActive": True}})
    res = runner.invoke(obs_app, ["--port", str(server.port), "record", "start"])
    assert res.exit_code == 0, res.output
    assert "recording started" in res.output


def test_status_tells_heard_from_stamped(tmp_path):
    b = _bridge(tmp_path, RecOBS(active=False))
    asyncio.run(b.moment({"token": TOKEN, "kind": "hacker"}))
    m = b.status()["last"]["moments"]
    assert m["heard"] == 1 and m["stamped"] == 0 and m["lastSkip"] == "not recording"
    b2 = _bridge(tmp_path, RecOBS(active=True))
    asyncio.run(b2.moment({"token": TOKEN, "kind": "hacker"}))
    assert b2.status()["last"]["moments"]["stamped"] == 1


def test_a_closed_obs_is_not_dialled_for_every_moment(tmp_path):
    from navig_obs.engine.errors import ObsConnectionError

    made = []

    class Down(RecOBS):
        def connect(self):
            made.append(1)
            raise ObsConnectionError("refused")

    b = _bridge(tmp_path, Down())
    for _ in range(5):
        asyncio.run(b.moment({"token": TOKEN, "kind": "hacker"}))
    assert len(made) == 1  # one try, then 5 s of "obs unavailable"
    m = b.status()["last"]["moments"]
    assert m["heard"] == 5 and m["lastSkip"] == "obs unavailable"
