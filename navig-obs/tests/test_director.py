"""The stream health director: sustained stream drops -> BRB, then back."""

from __future__ import annotations

import asyncio

from navig_obs.show import frames
from navig_obs.show.bridge import ShowBridge
from navig_obs.show.settings import ShowSettings, load_settings

BAD = {"network": (90, 300)}  # 30 % lost
CLEAN = {"network": (0, 300)}
LIVE = "02 The Architect"
BRB = "10 Signal Lost"


def run(d, seq, scene=LIVE, start=0.0, streaming=True):
    out = []
    for i, deltas in enumerate(seq):
        out.append(d.observe(deltas, streaming=streaming, scene=scene, now=start + i * 5))
    return out


def test_two_bad_looks_cut_to_brb_and_three_clean_bring_it_back():
    d = frames.Director()
    assert run(d, [BAD, BAD]) == [None, ("cut", BRB)]
    assert run(d, [CLEAN, CLEAN, CLEAN], scene=BRB, start=10) == [None, None, ("restore", LIVE)]


def test_one_bad_look_is_a_hiccup():
    d = frames.Director()
    assert run(d, [BAD, CLEAN, BAD, CLEAN]) == [None] * 4


def test_gpu_drops_never_move_the_stage():
    d = frames.Director()
    assert run(d, [{"render": (200, 300)}] * 4) == [None] * 4


def test_a_bad_look_while_on_brb_restarts_the_clean_count():
    d = frames.Director()
    run(d, [BAD, BAD])
    assert run(d, [CLEAN, CLEAN, BAD, CLEAN, CLEAN], scene=BRB, start=10) == [None] * 5
    assert run(d, [CLEAN], scene=BRB, start=40) == [("restore", LIVE)]


def test_the_operator_moving_the_stage_wins():
    d = frames.Director()
    run(d, [BAD, BAD])
    # on BRB the operator picks another scene: never yanked back
    assert run(d, [CLEAN, CLEAN, CLEAN, CLEAN], scene="17 Intermission", start=10) == [None] * 4
    assert not d.cut


def test_never_leaves_the_emergency_cut_and_not_when_off_air():
    d = frames.Director()
    assert run(d, [BAD, BAD, BAD], scene="14 Emergency Cut") == [None] * 3
    d2 = frames.Director()
    assert run(d2, [BAD, BAD], streaming=False) == [None, None]


def test_cooldown_after_a_restore():
    d = frames.Director()
    run(d, [BAD, BAD])
    run(d, [CLEAN] * 3, scene=BRB, start=10)  # restored at t=20
    assert run(d, [BAD, BAD], start=30) == [None, None]  # within 60 s: held
    # drops that last through the cooldown cut the moment it ends...
    assert run(d, [BAD], start=85) == [("cut", BRB)]
    d2 = frames.Director()
    run(d2, [BAD, BAD])
    run(d2, [CLEAN] * 3, scene=BRB, start=10)
    # ...but a clean look in between resets the streak
    assert run(d2, [BAD, CLEAN, BAD], start=85) == [None, None, None]


def test_off_by_default():
    assert ShowSettings().health_director is False


def test_setting_reads_the_brb_scene(monkeypatch):
    monkeypatch.setenv("NAVIG_OBS_SHOW_HEALTH_DIRECTOR", "true")
    monkeypatch.setenv("NAVIG_OBS_SHOW_BRB_SCENE", "18 Returning Soon")
    s = load_settings()
    assert s.health_director and s.brb_scene == "18 Returning Soon"


# ── the bridge acts ──────────────────────────────────────────────────────────
class LiveOBS:
    def __init__(self):
        self.scene = LIVE
        self.skip = 0
        self.total = 0
        self.calls = []

    def connect(self):
        pass

    def close(self):
        pass

    def request(self, rtype, data=None):
        self.calls.append((rtype, data))
        if rtype == "GetStats":
            return {"renderSkippedFrames": 0, "renderTotalFrames": self.total}
        if rtype == "GetStreamStatus":
            return {"outputActive": True, "outputSkippedFrames": self.skip, "outputTotalFrames": self.total}
        if rtype == "GetCurrentProgramScene":
            return {"currentProgramSceneName": self.scene}
        if rtype == "SetCurrentProgramScene":
            self.scene = data["sceneName"]
        return {}


def test_bridge_cuts_and_restores_on_a_live_obs():
    obs = LiveOBS()
    b = ShowBridge(
        ShowSettings(enabled=True, patch_feed="", civic_feed="", health_director=True),
        client_factory=lambda: obs,
    )
    t = 0.0
    for lost in (0, 90, 90):  # baseline, then two bad looks
        obs.skip += lost
        obs.total += 300
        asyncio.run(b.check_frames_once(now=t))
        t += 5
    assert obs.scene == BRB
    events = [d["requestData"]["event_data"] for r, d in obs.calls if r == "CallVendorRequest"]
    assert {"notice": "lag", "kind": "network"} in events
    for _ in range(3):  # three clean looks
        obs.total += 300
        asyncio.run(b.check_frames_once(now=t))
        t += 5
    assert obs.scene == LIVE
    assert b.status()["last"]["director"] == {"verb": "restore", "scene": LIVE, "ok": True}
    events = [d["requestData"]["event_data"] for r, d in obs.calls if r == "CallVendorRequest"]
    assert {"say": "back"} in events


def test_bridge_without_the_director_never_touches_the_scene():
    obs = LiveOBS()
    b = ShowBridge(ShowSettings(enabled=True, patch_feed="", civic_feed=""), client_factory=lambda: obs)
    for i, lost in enumerate((0, 90, 90, 90)):
        obs.skip += lost
        obs.total += 300
        asyncio.run(b.check_frames_once(now=i * 5.0))
    assert obs.scene == LIVE
    assert not any(r in ("SetCurrentProgramScene", "GetCurrentProgramScene") for r, _ in obs.calls)
