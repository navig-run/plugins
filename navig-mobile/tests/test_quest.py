"""Meta Quest engine + CLI — fake adb runner, no headset needed."""

from __future__ import annotations

from typer.testing import CliRunner

from navig_mobile.engine import quest as q

# Shape of real `adb devices -l` output (Quest 3 + Fire TV); identifiers are placeholders.
DEVICES = (
    "List of devices attached\n"
    "1WMHH0000000Q3         unauthorized transport_id:4\n"
    "192.168.1.50:5555      device product:raven model:AFTR device:raven transport_id:1\n"
    "192.168.1.20:5555      device product:eureka model:Quest_3 device:eureka transport_id:5\n"
)

# Real VrApi line from Beat Saber with the "quality" profile applied.
VRAPI = (
    "09-30 03:42:04.010  3209 10324 I VrApi   : FPS=120/120,Prd=25ms,Tear=0,"
    "Fov=2D,CPU4/GPU=4/4,1920/545MHz,Temp=37.0C/0.0C,TW=1.58ms\n"
    "09-30 03:42:05.010  3209 10324 I VrApi   : FPS=121/120,Prd=25ms,Fov=2D,"
    "CPU4/GPU=4/4,Temp=37.0C/0.0C\n"
)


class FakeQuest:
    """Minimal stand-in for a headset: honours setprop/getprop, scripted shells."""

    def __init__(self, block: set[str] | None = None):
        self.props: dict[str, str] = {}
        self.block = block or set()
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(args)
        if args[0] == "devices":
            return 0, DEVICES
        if args[0] != "shell":
            return 0, ""
        cmd = args[1]
        if cmd.startswith("setprop "):
            _, prop, val = cmd.split(" ", 2)
            if prop not in self.block:
                self.props[prop] = "" if val == "''" else val
            return 0, ""
        if cmd.startswith("getprop "):
            key = cmd.split(" ", 1)[1]
            return 0, {"ro.product.model": "Quest 3", "ro.product.device": "eureka"}.get(
                key, self.props.get(key, ""))
        if cmd.startswith("logcat"):
            return 0, VRAPI
        if cmd.startswith("ip -4"):
            return 0, "inet 192.168.1.20/24 scope global wlan0"
        return 0, ""


def test_parse_adb_devices_finds_only_quest_wifi_first():
    quests = q.find_quests(lambda a: (0, DEVICES))
    assert [d.serial for d in quests] == ["192.168.1.20:5555"]
    assert quests[0].state == "device" and quests[0].is_wifi
    # the USB row has no model: tag while unauthorized, so it is not mistaken for a Quest
    rows = q.parse_adb_devices(DEVICES)
    assert rows[0].state == "unauthorized"


def test_tweaks_props_skip_unset_and_round_trip():
    tw = q.Tweaks(refresh_rate=90, cpu_level=3)
    assert tw.props() == {"debug.oculus.refreshRate": "90", "debug.oculus.cpuLevel": "3"}
    assert q.Tweaks.from_dict(tw.to_dict()) == tw
    assert q.Tweaks.from_dict(q.Tweaks.from_dict(tw.to_dict()).to_dict()) == tw


def test_apply_tweaks_verifies_by_readback():
    dev = FakeQuest(block={"debug.oculus.gpuLevel"})
    res = q.apply_tweaks(dev, q.PROFILES["quality"])
    ok = {r.prop: r.ok for r in res}
    assert ok["debug.oculus.refreshRate"] is True
    assert ok["debug.oculus.gpuLevel"] is False  # rejected write is reported, not hidden


def test_tweaks_missing_after_reboot_then_present():
    dev = FakeQuest()
    tw = q.PROFILES["quality"]
    assert q.tweaks_missing(dev, tw)          # fresh boot: nothing set
    q.apply_tweaks(dev, tw)
    assert not q.tweaks_missing(dev, tw)


def test_reset_clears_everything():
    dev = FakeQuest()
    q.apply_tweaks(dev, q.PROFILES["quality"])
    assert all(r.ok and r.readback == "" for r in q.reset_tweaks(dev))


def test_parse_vrapi_reads_live_fps():
    s = q.parse_vrapi(VRAPI)
    assert (s[0].fps, s[0].target_fps, s[0].foveation, s[0].cpu_gpu, s[0].temp) == (
        "120", "120", "2D", "4/4", "37.0C")
    assert q.parse_vrapi("garbage\nno fps here") == []


def test_scrcpy_args_crop_and_record():
    a = q.scrcpy_args("192.168.1.20:5555", record="x.mp4", window=False, time_limit=8)
    assert "--crop=2064:2208:0:0" in a and "--record=x.mp4" in a and "--no-window" in a
    assert not any(x.startswith("--crop") for x in q.scrcpy_args("s", both_eyes=True))


def test_wifi_ip_and_is_quest():
    dev = FakeQuest()
    assert q.wifi_ip(dev) == "192.168.1.20"
    assert q.is_quest(dev)


def test_cli_tweaks_and_perf(monkeypatch):
    from navig_mobile.commands import quest as cmd
    from navig_mobile.commands.mobile import mobile_app

    dev = FakeQuest()
    saved = {}
    monkeypatch.setattr(cmd, "_runner", lambda serial: (dev, "192.168.1.20:5555"))
    monkeypatch.setattr(cmd.config, "set", lambda k, v: saved.__setitem__(k, v))
    monkeypatch.setattr(cmd.config, "get", lambda k, d=None: saved.get(k, d))
    r = CliRunner().invoke(mobile_app, ["quest", "tweaks", "quality", "--hz", "90"])
    assert r.exit_code == 0, r.output
    assert dev.props["debug.oculus.refreshRate"] == "90"
    assert saved["quest.tweaks"]["refresh_rate"] == 90       # remembered for `watch`
    r = CliRunner().invoke(mobile_app, ["quest", "perf", "--json"])
    assert r.exit_code == 0 and '"fps": "120"' in r.output
    r = CliRunner().invoke(mobile_app, ["quest", "tweaks", "nope"])
    assert r.exit_code == 2


def test_offline_transport_raises_instead_of_returning_error_text():
    import pytest

    def offline(args):
        return 1, "adb.EXE: device offline"

    with pytest.raises(q.QuestOffline):
        q.shell(offline, "getprop ro.product.model")
    with pytest.raises(q.QuestOffline):
        q.status(offline)



# ── sign-in hook: Task Scheduler when it allows it, a Startup shortcut when it does not ─

def _proc(returncode: int, stdout: str = "", stderr: str = ""):
    from types import SimpleNamespace

    return SimpleNamespace(args=["schtasks"], returncode=returncode,
                           stdout=stdout, stderr=stderr)


def _watch_cli(monkeypatch, tmp_path, extra, run, fallback=None):
    """Invoke `quest watch <args>` with Task Scheduler, PowerShell and Startup stubbed.

    Returns ``(result, argvs_seen, fallback_used)``.
    """
    from types import SimpleNamespace

    from navig_mobile.commands import quest as cmd
    from navig_mobile.commands.mobile import mobile_app

    calls: list[list[str]] = []
    used: list[bool] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return run(argv)

    monkeypatch.setattr(cmd, "sys", SimpleNamespace(platform="win32",
                                                    executable=r"C:\Python\python.exe"))
    monkeypatch.setattr(cmd, "_startup_dir", lambda: tmp_path)
    monkeypatch.setattr(cmd, "subprocess", SimpleNamespace(run=fake_run))
    if fallback is not None:
        monkeypatch.setattr(cmd, "_install_startup", lambda: used.append(True) or fallback)
    return CliRunner().invoke(mobile_app, ["quest", "watch", *extra]), calls, used


def test_watch_install_falls_back_to_startup_when_logon_task_denied(monkeypatch, tmp_path):
    """A non-elevated shell cannot register an ONLOGON task — the hook must still install."""
    r, calls, used = _watch_cli(
        monkeypatch, tmp_path, ["--install"],
        lambda argv: _proc(1, stderr="ERROR: Access is denied."),
        fallback=(True, str(tmp_path / "navig quest watch.lnk")))
    assert r.exit_code == 0, r.output
    assert calls[0][:2] == ["schtasks", "/Create"] and used == [True]
    assert "Startup" in r.output


def test_watch_install_prefers_the_scheduled_task(monkeypatch, tmp_path):
    stale = tmp_path / "navig quest watch.lnk"      # a shortcut from an earlier attempt
    stale.write_text("x")
    r, calls, used = _watch_cli(monkeypatch, tmp_path, ["--install"], lambda argv: _proc(0))
    assert r.exit_code == 0, r.output
    assert calls[0][:2] == ["schtasks", "/Create"] and used == []
    assert "sign-in" in r.output
    assert "/SC" in calls[0] and "ONLOGON" in calls[0]
    assert not stale.exists()                       # one hook, not two


def test_watch_install_fails_when_no_hook_can_be_created(monkeypatch, tmp_path):
    r, calls, used = _watch_cli(
        monkeypatch, tmp_path, ["--install"],
        lambda argv: _proc(1, stderr="ERROR: Access is denied."),
        fallback=(False, "shortcut not written"))
    assert r.exit_code == 1


def test_watch_uninstall_removes_task_and_shortcut(monkeypatch, tmp_path):
    from navig_mobile.commands import quest as cmd

    lnk = tmp_path / "navig quest watch.lnk"
    lnk.write_text("x")
    r, calls, used = _watch_cli(monkeypatch, tmp_path, ["--uninstall"],
                                lambda argv: _proc(0))
    assert r.exit_code == 0, r.output
    assert calls[0][:2] == ["schtasks", "/Delete"]
    assert not lnk.exists() and "Removed" in r.output
    assert cmd._lnk_path() == lnk


def test_watch_uninstall_says_so_when_nothing_is_installed(monkeypatch, tmp_path):
    r, calls, used = _watch_cli(monkeypatch, tmp_path, ["--uninstall"],
                                lambda argv: _proc(1, stderr="not found"))
    assert r.exit_code == 0 and "Nothing" in r.output


def test_install_startup_encodes_a_hidden_watch_command(monkeypatch, tmp_path):
    """The shortcut writer must hand PowerShell a decodable script that points at watch."""
    import base64
    from types import SimpleNamespace

    from navig_mobile.commands import quest as cmd

    seen: dict[str, str] = {}

    def fake_run(argv, **kwargs):
        assert argv[0] == "powershell.exe" and "-EncodedCommand" in argv
        seen["script"] = base64.b64decode(argv[-1]).decode("utf-16-le")
        (tmp_path / "navig quest watch.lnk").write_text("")
        return _proc(0)

    monkeypatch.setattr(cmd, "_startup_dir", lambda: tmp_path)
    monkeypatch.setattr(cmd, "subprocess", SimpleNamespace(run=fake_run))
    ok, detail = cmd._install_startup()
    assert ok and detail.endswith("navig quest watch.lnk")
    script = seen["script"]
    assert "WScript.Shell" in script and "CreateShortcut" in script
    assert "quest watch" in script and "-WindowStyle Hidden" in script

    monkeypatch.setattr(cmd, "subprocess",
                        SimpleNamespace(run=lambda *a, **k: _proc(1, stderr="boom")))
    ok, detail = cmd._install_startup()
    assert not ok and "boom" in detail


def test_watch_target_is_powershell_running_watch_hidden():
    from navig_mobile.commands import quest as cmd

    prog, args = cmd._watch_target()
    assert prog == "powershell.exe"
    assert "-NoProfile" in args and "-WindowStyle Hidden" in args and "quest watch" in args
