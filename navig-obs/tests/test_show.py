"""Show events: feed diff, cue book, OBS moves, the bridge, and the CLI."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from navig_obs.commands.obs import obs_app
from navig_obs.engine.errors import ObsConnectionError
from navig_obs.show import actions, feed
from navig_obs.show.bridge import ShowBridge
from navig_obs.show.cues import Cue, CueBook, CueBookError
from navig_obs.show.settings import ShowSettings, load_settings

runner = CliRunner()


def _doc(*seqs: int, compiling: dict | None = None) -> dict:
    return {
        "patches": [
            {
                "seq": s,
                "version": "1.6.5",
                "headline": f"Patch {s} headline",
                "publicCount": s % 5,
            }
            for s in seqs
        ],
        "nextCursor": None,
        "compiling": compiling,
    }


def _write_book(tmp_path, cues: list[dict]) -> object:
    path = tmp_path / "cues.json"
    path.write_text(json.dumps({"contractVersion": 1, "cues": cues}), encoding="utf-8")
    return path


def _cue(cue_id: str, cooldown: int = 0) -> dict:
    return {
        "cueId": cue_id,
        "file": f"sfx/{cue_id}.wav",
        "trigger": "event",
        "lufsTarget": -16,
        "cooldownMs": cooldown,
        "provenance": {"origin": "generated: test", "date": "2026-10-15"},
    }


class Recorder:
    def __init__(
        self,
        fail: Exception | None = None,
        inputs: list[str] | None = None,
        items: dict | None = None,
    ):
        self.calls: list[tuple[str, dict | None]] = []
        self.fail = fail
        self.inputs = inputs or []
        self.items = items or {}

    def connect(self) -> None:
        if self.fail:
            raise self.fail

    def close(self) -> None:
        pass

    def request(self, rtype: str, data: dict | None = None) -> dict:
        self.calls.append((rtype, data))
        if rtype == "GetInputList":
            return {"inputs": [{"inputName": n} for n in self.inputs]}
        if rtype == "GetSceneItemList":
            return {
                "sceneItems": [
                    {"sourceName": n} for n in self.items.get(data["sceneName"], [])
                ]
            }
        return {}


# ── feed diff ────────────────────────────────────────────────────────────────
def test_first_look_seeds_without_toasts():
    toasts, state = feed.diff(None, _doc(220, 219))
    assert toasts == []
    assert state.seq == 220


def test_new_patch_toasts_only_the_newest():
    toasts, state = feed.diff(feed.FeedState(seq=218), _doc(221, 220, 219))
    assert [t["kind"] for t in toasts] == ["patch"]
    assert toasts[0]["seq"] == 221 and toasts[0]["headline"] == "Patch 221 headline"
    assert state.seq == 221


def test_compiling_appears_then_grows():
    comp = {"since": "2026-10-15T10:00:00Z", "publishAt": "x", "count": 2}
    toasts, state = feed.diff(feed.FeedState(seq=220), _doc(220, compiling=comp))
    assert toasts == [{"kind": "compiling", "count": 2, "since": comp["since"]}]
    toasts, state = feed.diff(state, _doc(220, compiling=comp))
    assert toasts == []  # nothing changed → nothing shown
    toasts, _ = feed.diff(state, _doc(220, compiling={**comp, "count": 3}))
    assert toasts and toasts[0]["count"] == 3


def test_feed_state_never_moves_backwards():
    _, state = feed.diff(feed.FeedState(seq=230), _doc(220))
    assert state.seq == 230


def test_state_round_trip(tmp_path):
    path = tmp_path / "s" / "feed-state.json"
    st = feed.FeedState(seq=7, compiling_since="a", compiling_count=2)
    feed.save_state(path, st)
    assert feed.load_state(path) == st
    path.write_text("{broken", encoding="utf-8")
    assert feed.load_state(path) is None


# ── cue book ─────────────────────────────────────────────────────────────────
def test_cue_book_resolves_relative_files_and_cooldown(tmp_path):
    book = CueBook.load(_write_book(tmp_path, [_cue("sale-ring", cooldown=1500)]))
    cue = book.get("sale-ring")
    assert (
        cue is not None and cue.path == (tmp_path / "sfx" / "sale-ring.wav").resolve()
    )
    assert book.ready("sale-ring", now=10.0) is True
    assert book.ready("sale-ring", now=11.0) is False  # inside 1.5 s
    assert book.ready("sale-ring", now=11.6) is True
    assert book.ready("unknown") is False


def test_cue_without_provenance_is_refused(tmp_path):
    bad = _cue("x")
    bad["provenance"] = {"origin": ""}
    with pytest.raises(CueBookError):
        CueBook.load(_write_book(tmp_path, [bad]))
    with pytest.raises(CueBookError):
        CueBook.load(tmp_path / "missing.json")


# ── OBS moves ────────────────────────────────────────────────────────────────
def test_play_cue_is_one_input_set_then_restart(tmp_path):
    rec = Recorder()
    actions.play_cue(rec, "SCHEMA · Cue", Cue("sale-ring", tmp_path / "a.wav", 0))
    assert [c[0] for c in rec.calls] == ["SetInputSettings", "TriggerMediaInputAction"]
    assert rec.calls[0][1]["inputSettings"]["local_file"] == str(tmp_path / "a.wav")
    assert rec.calls[1][1]["mediaAction"] == actions.RESTART


def test_toast_is_an_obs_browser_vendor_event():
    rec = Recorder()
    actions.emit_toast(rec, "schema-toast", {"kind": "patch", "seq": 1})
    rtype, data = rec.calls[0]
    assert rtype == "CallVendorRequest"
    assert data["vendorName"] == "obs-browser" and data["requestType"] == "emit_event"
    assert data["requestData"]["event_name"] == "schema-toast"


def test_ensure_cue_input_creates_once_then_places():
    rec = Recorder(items={"B": ["SCHEMA · Cue"]})
    out = actions.ensure_cue_input(rec, "SCHEMA · Cue", ["A", "B", "C"])
    assert out == {"created": ["A"], "placed": ["C"]}
    kinds = [c[0] for c in rec.calls]
    assert kinds.count("CreateInput") == 1 and kinds.count("CreateSceneItem") == 1


# ── the bridge ───────────────────────────────────────────────────────────────
def _bridge(tmp_path, rec, docs):
    book = _write_book(
        tmp_path, [_cue("sale-ring", cooldown=60_000), _cue("patch-landed")]
    )
    settings = ShowSettings(
        enabled=True, patch_feed="https://feed.test", cues_file=book
    )
    it = iter(docs)
    b = ShowBridge(
        settings,
        client_factory=lambda: rec,
        fetcher=lambda _url: next(it),
        state_path=tmp_path / "st.json",
    )
    b.reload_cues()
    return b


def test_sale_notification_rings_once_inside_cooldown(tmp_path):
    rec = Recorder()
    b = _bridge(tmp_path, rec, [])
    sale = SimpleNamespace(payload={"type": "sale", "title": "x"})
    other = SimpleNamespace(payload={"type": "deploy"})
    asyncio.run(b.on_notification(sale))
    asyncio.run(b.on_notification(sale))  # cooldown swallows the second
    asyncio.run(b.on_notification(other))
    assert [c[0] for c in rec.calls] == [
        "SetInputSettings",
        "TriggerMediaInputAction",
        "CallVendorRequest",  # the pigeon's windfall, once
    ]


def test_sale_sends_the_pigeon_the_act_and_nothing_else(tmp_path):
    # Where a sale happened must never reach the screen: no store, buyer,
    # amount or title from the notification travels to the page.
    rec = Recorder()
    b = _bridge(tmp_path, rec, [])
    sale = SimpleNamespace(
        payload={"type": "sale", "title": "Store X: 1x Pro 9.99 EUR", "body": "buyer"}
    )
    asyncio.run(b.on_notification(sale))
    vendor = [c for c in rec.calls if c[0] == "CallVendorRequest"]
    assert len(vendor) == 1
    rd = vendor[0][1]["requestData"]
    assert rd == {"event_name": "pigeon", "event_data": {"act": "windfall"}}


def test_windfall_cools_down_then_fires_again(tmp_path):
    rec = Recorder()
    b = _bridge(tmp_path, rec, [])
    assert asyncio.run(b.windfall(now=1000.0)) is True
    assert asyncio.run(b.windfall(now=1030.0)) is False  # inside the cooldown
    assert asyncio.run(b.windfall(now=1000.0 + 91)) is True


def test_poll_seeds_then_toasts_with_cue(tmp_path):
    rec = Recorder()
    b = _bridge(tmp_path, rec, [_doc(220), _doc(221, 220)])
    assert asyncio.run(b.poll_once()) == []  # seed
    assert rec.calls == []
    toasts = asyncio.run(b.poll_once())
    assert toasts[0]["seq"] == 221
    assert [c[0] for c in rec.calls] == [
        "CallVendorRequest",
        "SetInputSettings",
        "TriggerMediaInputAction",
    ]


def test_obs_down_is_a_quiet_no_op_and_feed_still_advances(tmp_path):
    rec = Recorder(fail=ObsConnectionError("cannot reach OBS"))
    b = _bridge(tmp_path, rec, [_doc(220), _doc(221)])
    asyncio.run(b.poll_once())
    asyncio.run(b.poll_once())
    assert feed.load_state(tmp_path / "st.json").seq == 221  # no backlog replay later
    assert b.status()["last"]["obs"] == "cannot reach OBS"


def test_feed_failure_is_a_skipped_beat(tmp_path):
    def boom(_url):
        raise OSError("offline")

    settings = ShowSettings(
        enabled=True, patch_feed="https://feed.test", cues_file=tmp_path / "none.json"
    )
    b = ShowBridge(
        settings, client_factory=Recorder, fetcher=boom, state_path=tmp_path / "st.json"
    )
    assert asyncio.run(b.poll_once()) == []
    assert b.status()["last"]["feed"] == "offline"


def test_settings_default_off_and_env_on(monkeypatch):
    monkeypatch.delenv("NAVIG_OBS_SHOW_ENABLED", raising=False)
    monkeypatch.setattr("navig_obs.show.settings._cfg", lambda _k: None)
    assert load_settings().enabled is False
    monkeypatch.setenv("NAVIG_OBS_SHOW_ENABLED", "1")
    monkeypatch.setenv("NAVIG_OBS_SHOW_POLL_SECONDS", "3")
    s = load_settings()
    assert s.enabled is True and s.poll_seconds == 15  # floor


# ── CLI through a fake OBS ───────────────────────────────────────────────────
def test_cli_toast_reaches_obs(obs_factory):
    server = obs_factory(requests={"CallVendorRequest": {}})
    result = runner.invoke(
        obs_app,
        [
            "--port",
            str(server.port),
            "show",
            "toast",
            "--seq",
            "221",
            "--headline",
            "The Rooms remember",
        ],
    )
    assert result.exit_code == 0, result.output
    rtype, data = server.received[-1]
    assert rtype == "CallVendorRequest"
    assert data["requestData"]["event_data"] == {
        "kind": "patch",
        "count": 0,
        "seq": 221,
        "headline": "The Rooms remember",
    }


# ── the civic feed (the Grid Pigeon) ─────────────────────────────────────────
from navig_obs.show import civic  # noqa: E402


def _civic(*stamps: str, kind: str = "ignition") -> dict:
    return {
        "data": [
            {"id": s, "kind": kind, "occurredAt": s, "zone": None, "payload": {}}
            for s in stamps
        ],
        "serverTime": "2026-10-15T12:00:00Z",
    }


def test_civic_first_look_seeds_without_events():
    events, state = civic.diff(None, _civic("2026-10-15T10:00:00Z"))
    assert events == [] and state.since == "2026-10-15T10:00:00Z"


def test_civic_new_rows_fire_oldest_first():
    prev = civic.CivicState("2026-10-15T10:00:00Z")
    doc = _civic("2026-10-15T10:02:00Z", "2026-10-15T10:01:00Z", "2026-10-15T10:00:00Z")
    events, state = civic.diff(prev, doc)
    assert [e["occurredAt"] for e in events] == [
        "2026-10-15T10:01:00Z",
        "2026-10-15T10:02:00Z",
    ]
    assert state.since == "2026-10-15T10:02:00Z"


def test_civic_downtime_never_floods():
    prev = civic.CivicState("2026-10-15T00:00:00Z")
    doc = _civic(*(f"2026-10-15T0{h}:00:00Z" for h in range(1, 9)))
    events, _ = civic.diff(prev, doc)
    assert len(events) == civic.MAX_BURST
    assert events[-1]["occurredAt"] == "2026-10-15T08:00:00Z"


def test_civic_cursor_never_moves_backwards():
    prev = civic.CivicState("2026-10-15T10:00:00Z")
    _, state = civic.diff(prev, {"data": []})
    assert state.since == "2026-10-15T10:00:00Z"


def test_civic_url_seeds_with_one_row_then_asks_since():
    assert civic.feed_url("https://x/api/civic/events", None).endswith("?limit=1")
    url = civic.feed_url("https://x/api/civic/events", civic.CivicState("2026-10-15T10:00:00Z"))
    assert "since=2026-10-15T10%3A00%3A00Z" in url and "limit=20" in url


def test_civic_state_round_trip(tmp_path):
    p = tmp_path / "c.json"
    civic.save_state(p, civic.CivicState("2026-10-15T10:00:00Z"))
    assert civic.load_state(p) == civic.CivicState("2026-10-15T10:00:00Z")
    assert civic.load_state(tmp_path / "missing.json") is None


def test_bridge_civic_seeds_then_emits_pigeon_civic(tmp_path):
    rec = Recorder()
    settings = ShowSettings(enabled=True, patch_feed="", civic_feed="https://civic.test")
    docs = iter([_civic("2026-10-15T10:00:00Z"), _civic("2026-10-15T10:05:00Z")])
    b = ShowBridge(
        settings, client_factory=lambda: rec, fetcher=lambda _u: next(docs),
        state_path=tmp_path / "st.json",
    )
    assert asyncio.run(b.poll_civic_once()) == []  # seed
    assert rec.calls == []
    fired = asyncio.run(b.poll_civic_once())
    assert len(fired) == 1
    rtype, data = rec.calls[0]
    assert rtype == "CallVendorRequest"
    assert data["requestData"]["event_name"] == "pigeonCivic"
    assert data["requestData"]["event_data"]["kind"] == "ignition"


def test_bridge_civic_obs_down_still_advances(tmp_path):
    rec = Recorder(fail=ObsConnectionError("cannot reach OBS"))
    settings = ShowSettings(enabled=True, patch_feed="", civic_feed="https://civic.test")
    docs = iter([_civic("2026-10-15T10:00:00Z"), _civic("2026-10-15T10:05:00Z")])
    b = ShowBridge(
        settings, client_factory=lambda: rec, fetcher=lambda _u: next(docs),
        state_path=tmp_path / "st.json",
    )
    asyncio.run(b.poll_civic_once())
    asyncio.run(b.poll_civic_once())
    assert civic.load_state(tmp_path / "civic-state.json").since == "2026-10-15T10:05:00Z"


def test_cli_pigeon_emits_the_beat(obs_factory):
    server = obs_factory(requests={"CallVendorRequest": {}})
    result = runner.invoke(
        obs_app, ["--port", str(server.port), "show", "pigeon", "passport", "--mode", "courier"]
    )
    assert result.exit_code == 0, result.output
    rtype, data = server.received[-1]
    assert rtype == "CallVendorRequest"
    assert data["requestData"] == {
        "event_name": "pigeon",
        "event_data": {"act": "passport", "mode": "courier"},
    }


def test_cli_pigeon_refuses_an_unknown_mode():
    result = runner.invoke(obs_app, ["show", "pigeon", "peck", "--mode", "furious"])
    assert result.exit_code == 2


def test_cli_pigeon_needs_a_beat_or_a_mode():
    result = runner.invoke(obs_app, ["show", "pigeon"])
    assert result.exit_code == 2
