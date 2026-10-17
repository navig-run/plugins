"""Dropped frames → the pigeon's lag line, and `navig obs show pigeon --say`."""

from __future__ import annotations

import asyncio

from typer.testing import CliRunner

from navig_obs.commands.obs import obs_app
from navig_obs.engine.errors import ObsConnectionError
from navig_obs.show import frames
from navig_obs.show.bridge import ShowBridge
from navig_obs.show.settings import ShowSettings

runner = CliRunner()


# ── the decision ─────────────────────────────────────────────────────────────
def test_first_sample_is_only_a_baseline():
    w = frames.FrameWatch()
    assert w.update({"render": (500, 10_000)}, now=0) is None


def test_a_burst_the_audience_sees_fires_once():
    w = frames.FrameWatch()
    w.update({"render": (0, 1000), "encode": (0, 1000)}, now=0)
    # 5 s at 60 fps = 300 frames; 60 of them lost = 20 %
    assert w.update({"render": (60, 1300), "encode": (0, 1300)}, now=5) == "render"


def test_a_few_lost_frames_are_noise():
    w = frames.FrameWatch()
    w.update({"encode": (0, 1000)}, now=0)
    assert w.update({"encode": (10, 1300)}, now=5) is None  # under MIN_FRAMES


def test_many_frames_but_a_tiny_share_is_noise():
    w = frames.FrameWatch()
    w.update({"render": (0, 0)}, now=0)
    # 40 lost of 100 000 drawn — a long gap between samples, not a hitch
    assert w.update({"render": (40, 100_000)}, now=5) is None


def test_cooldown_keeps_him_from_nagging():
    w = frames.FrameWatch(cooldown_s=180)
    w.update({"network": (0, 0)}, now=0)
    assert w.update({"network": (100, 300)}, now=5) == "network"
    assert w.update({"network": (200, 600)}, now=10) is None
    assert w.update({"network": (300, 900)}, now=200) == "network"


def test_a_counter_reset_is_a_new_baseline_not_a_burst():
    w = frames.FrameWatch()
    w.update({"network": (900, 50_000)}, now=0)
    # a new stream: OBS's per-output counters start again at zero
    assert w.update({"network": (0, 300)}, now=5) is None
    assert w.update({"network": (80, 600)}, now=10) == "network"


def test_the_worst_kind_wins():
    w = frames.FrameWatch()
    w.update({"render": (0, 0), "encode": (0, 0)}, now=0)
    assert w.update({"render": (40, 300), "encode": (120, 300)}, now=5) == "encode"


def test_sample_reads_obs_fields_and_network_only_while_live():
    stats = {
        "renderSkippedFrames": 3, "renderTotalFrames": 900,
        "outputSkippedFrames": 7, "outputTotalFrames": 880,
    }
    off = frames.sample_from(stats, {"outputActive": False, "outputSkippedFrames": 99})
    assert off == {"render": (3, 900), "encode": (7, 880)}
    live = frames.sample_from(
        stats, {"outputActive": True, "outputSkippedFrames": 5, "outputTotalFrames": 870}
    )
    assert live["network"] == (5, 870)
    assert frames.sample_from({"renderSkippedFrames": None}, None)["render"] == (0, 0)


# ── the bridge ───────────────────────────────────────────────────────────────
class StatsOBS:
    def __init__(self, samples, fail=None):
        self.samples = iter(samples)
        self.current = None
        self.calls = []
        self.fail = fail

    def connect(self):
        if self.fail:
            raise self.fail

    def close(self):
        pass

    def request(self, rtype, data=None):
        self.calls.append((rtype, data))
        if rtype == "GetStats":
            self.current = next(self.samples)
            return self.current
        if rtype == "GetStreamStatus":
            return {"outputActive": False}
        return {}


def test_bridge_tells_the_pigeon_about_a_burst():
    obs = StatsOBS([
        {"renderSkippedFrames": 0, "renderTotalFrames": 1000},
        {"renderSkippedFrames": 90, "renderTotalFrames": 1300},
    ])
    b = ShowBridge(ShowSettings(enabled=True, patch_feed="", civic_feed=""),
                   client_factory=lambda: obs)
    assert asyncio.run(b.check_frames_once(now=0)) is None
    assert asyncio.run(b.check_frames_once(now=5)) == "render"
    sent = [d for r, d in obs.calls if r == "CallVendorRequest"]
    assert sent[-1]["requestData"] == {
        "event_name": "pigeon",
        "event_data": {"notice": "lag", "kind": "render"},
    }


def test_bridge_with_obs_closed_is_a_quiet_skip():
    obs = StatsOBS([], fail=ObsConnectionError("cannot reach OBS"))
    b = ShowBridge(ShowSettings(enabled=True, patch_feed="", civic_feed=""),
                   client_factory=lambda: obs)
    assert asyncio.run(b.check_frames_once(now=0)) is None


def test_frames_watch_is_on_by_default_and_can_be_turned_off(monkeypatch):
    from navig_obs.show.settings import load_settings

    monkeypatch.delenv("NAVIG_OBS_SHOW_FRAMES_WATCH", raising=False)
    assert ShowSettings().frames_watch is True
    monkeypatch.setenv("NAVIG_OBS_SHOW_FRAMES_WATCH", "false")
    assert load_settings().frames_watch is False


# ── the CLI: --say ───────────────────────────────────────────────────────────
def test_cli_pigeon_says_a_line(obs_factory):
    server = obs_factory(requests={"CallVendorRequest": {}})
    result = runner.invoke(
        obs_app, ["--port", str(server.port), "show", "pigeon", "--say", "clock", "--index", "2"]
    )
    assert result.exit_code == 0, result.output
    rtype, data = server.received[-1]
    assert rtype == "CallVendorRequest"
    assert data["requestData"] == {
        "event_name": "pigeon",
        "event_data": {"say": "clock", "index": 2},
    }


def test_cli_pigeon_say_without_index_lets_him_choose(obs_factory):
    server = obs_factory(requests={"CallVendorRequest": {}})
    result = runner.invoke(obs_app, ["--port", str(server.port), "show", "pigeon", "--say", "hydrate"])
    assert result.exit_code == 0, result.output
    assert server.received[-1][1]["requestData"]["event_data"] == {"say": "hydrate"}


def test_cli_pigeon_say_refuses_bad_input():
    assert runner.invoke(obs_app, ["show", "pigeon", "--say", "Not A Moment"]).exit_code == 2
    assert runner.invoke(obs_app, ["show", "pigeon", "peck", "--say", "clock"]).exit_code == 2
    assert runner.invoke(obs_app, ["show", "pigeon", "--index", "1"]).exit_code == 2
    assert runner.invoke(obs_app, ["show", "pigeon", "--say", "clock", "--index", "-1"]).exit_code == 2


def test_cli_pigeon_help_lists_the_moments():
    out = runner.invoke(obs_app, ["show", "pigeon", "--help"]).output
    assert "hydrate" in out and "--say" in out


# ── one connection, not one per look ─────────────────────────────────────────
def test_the_watch_keeps_one_connection_across_looks():
    # 2026-10-16: a connect per 5 s look was 1,411 of 2,090 lines of OBS's log.
    made = []
    samples = [{"renderSkippedFrames": 0, "renderTotalFrames": 100 * i} for i in range(1, 6)]
    obs = StatsOBS(samples)

    def factory():
        made.append(1)
        return obs

    b = ShowBridge(ShowSettings(enabled=True, patch_feed="", civic_feed=""), client_factory=factory)
    for i in range(5):
        asyncio.run(b.check_frames_once(now=i * 5))
    assert len(made) == 1
    assert sum(1 for r, _ in obs.calls if r == "GetStats") == 5


def test_a_failed_look_drops_the_connection_and_the_next_reconnects():
    class Flaky(StatsOBS):
        def request(self, rtype, data=None):
            if rtype == "GetStats" and self.fail_next:
                self.fail_next = False
                raise ObsConnectionError("socket closed")
            return super().request(rtype, data)

    made = []
    obs = Flaky([{"renderSkippedFrames": 0, "renderTotalFrames": 100}] * 3)
    obs.fail_next = True
    closed = []
    obs.close = lambda: closed.append(1)

    def factory():
        made.append(1)
        return obs

    b = ShowBridge(ShowSettings(enabled=True, patch_feed="", civic_feed=""), client_factory=factory)
    assert asyncio.run(b.check_frames_once(now=0)) is None  # the socket died
    assert closed == [1]
    asyncio.run(b.check_frames_once(now=1))  # inside the 5 s down window: no dial
    assert len(made) == 1
    b._obs_down_until = 0.0  # the window has passed
    asyncio.run(b.check_frames_once(now=5))  # a fresh connection
    asyncio.run(b.check_frames_once(now=10))  # and it is kept
    assert len(made) == 2
