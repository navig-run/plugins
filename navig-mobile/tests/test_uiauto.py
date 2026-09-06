"""App UI automation (agent-device) — wrapper, detection, and CLI wiring tests.

Everything is exercised without agent-device installed: the wrapper's subprocess
call is monkeypatched, and the CLI verbs are driven with a fake engine (mirrors
test_devtools' approach for frida)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from navig_mobile.commands.mobile import mobile_app
from navig_mobile.engine.uiauto import agent_device as ad

runner = CliRunner()


class _Proc:
    """A faithful stand-in for what `subprocess.run` returns — including `args`.

    A real CompletedProcess always carries `args`, so a fake that omits it is claiming
    something untrue about the API under test. That gap is invisible until a caller
    touches the field: `proc_text.decode_console_result` passes it through, and these
    tests failed with `AttributeError: '_Proc' object has no attribute 'args'` while the
    production path was fine.
    """

    def __init__(self, rc: int = 0, out: str = "", err: str = "", args=None):
        self.args = args if args is not None else []
        self.returncode = rc
        self.stdout = out
        self.stderr = err


# ── wrapper: detection + arg-building ────────────────────────────────────────

def test_available_reflects_which(monkeypatch):
    monkeypatch.setattr(ad.shutil, "which",
                        lambda n: "/usr/bin/agent-device" if n == "agent-device" else None)
    assert ad.available() is True
    monkeypatch.setattr(ad.shutil, "which", lambda n: None)
    assert ad.available() is False


def test_run_raises_when_missing(monkeypatch):
    monkeypatch.setattr(ad, "_exe", lambda: None)
    with pytest.raises(ad.AgentDeviceUnavailable):
        ad._run(["doctor"])


def test_snapshot_builds_args(monkeypatch):
    seen: dict = {}

    # **kwargs, not a fixed signature: a fake that names every keyword is asserting
    # the CALL'S SPELLING, so an unrelated kwarg change (dropping text=True when the
    # decode moved out of subprocess) breaks it with a TypeError that says nothing
    # about the behaviour under test.
    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _Proc(0, "@e1 button 'Login'")

    monkeypatch.setattr(ad, "_exe", lambda: "agent-device")
    monkeypatch.setattr(ad.subprocess, "run", fake_run)
    rc, out = ad.snapshot(interactive=True, json_out=True)
    assert rc == 0 and "@e1" in out
    assert seen["argv"] == ["agent-device", "snapshot", "-i", "--json"]


def test_snapshot_full_omits_interactive_flag(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(ad, "_exe", lambda: "agent-device")
    monkeypatch.setattr(ad.subprocess, "run",
                        lambda argv, **k: (seen.__setitem__("argv", argv), _Proc(0, ""))[1])
    ad.snapshot(interactive=False)
    assert seen["argv"] == ["agent-device", "snapshot"]


def test_open_threads_platform_and_device(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(ad, "_exe", lambda: "agent-device")
    monkeypatch.setattr(ad.subprocess, "run",
                        lambda argv, **k: (seen.__setitem__("argv", argv), _Proc(0, "ok"))[1])
    ad.open_app("MyApp", "android", "emulator-5554")
    assert seen["argv"] == ["agent-device", "open", "MyApp",
                            "--platform", "android", "--device", "emulator-5554"]


def test_tap_and_fill_args(monkeypatch):
    calls: list = []
    monkeypatch.setattr(ad, "_exe", lambda: "agent-device")
    monkeypatch.setattr(ad.subprocess, "run",
                        lambda argv, **k: (calls.append(argv), _Proc(0, ""))[1])
    ad.tap("@e2")
    ad.fill("@e3", "hi@example.com")
    assert calls[0] == ["agent-device", "tap", "@e2"]
    assert calls[1] == ["agent-device", "fill", "@e3", "hi@example.com"]


# ── toolchain detection ──────────────────────────────────────────────────────

def test_detect_reports_agent_device_and_node(monkeypatch):
    import navig_mobile.tools as tools_mod

    def fake_which(names, extra_dirs=None):
        if "agent-device" in names or "node" in names:
            return Path("/usr/bin") / names[0]
        return None

    monkeypatch.setattr(tools_mod, "_which", fake_which)
    tc = tools_mod.detect()
    assert tc.get("agent-device").found is True
    assert tc.get("node").found is True
    assert tc.uiauto_ready is True


def test_uiauto_not_ready_when_absent(monkeypatch):
    import navig_mobile.tools as tools_mod

    monkeypatch.setattr(tools_mod, "_which", lambda names, extra_dirs=None: None)
    tc = tools_mod.detect()
    assert tc.uiauto_ready is False
    assert tc.get("agent-device").install_hint  # guidance is present


# ── CLI wiring ───────────────────────────────────────────────────────────────

def test_cli_ui_missing_binary_guides(monkeypatch):
    monkeypatch.setattr(ad, "available", lambda: False)
    r = runner.invoke(mobile_app, ["ui", "doctor"])
    assert r.exit_code == 127
    assert "agent-device not found" in r.stdout


def test_cli_ui_doctor_runs(monkeypatch):
    monkeypatch.setattr(ad, "available", lambda: True)
    monkeypatch.setattr(ad, "version", lambda: "0.19.1")
    monkeypatch.setattr(ad, "doctor", lambda: (0, "all good"))
    r = runner.invoke(mobile_app, ["ui", "doctor"])
    assert r.exit_code == 0
    assert "0.19.1" in r.stdout and "all good" in r.stdout


def test_cli_ui_snapshot(monkeypatch):
    monkeypatch.setattr(ad, "available", lambda: True)
    monkeypatch.setattr(ad, "snapshot", lambda **k: (0, "@e1 button"))
    r = runner.invoke(mobile_app, ["ui", "snapshot", "-i"])
    assert r.exit_code == 0 and "@e1" in r.stdout


def test_cli_ui_open_threads_flags(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(ad, "available", lambda: True)

    def fake_open(app, platform, device):
        seen["call"] = (app, platform, device)
        return (0, "opened")

    monkeypatch.setattr(ad, "open_app", fake_open)
    # an emulator target isn't consent-gated, so this exercises flag-threading cleanly
    r = runner.invoke(mobile_app, ["ui", "open", "MyApp", "-p", "android", "-u", "emulator-5554"])
    assert r.exit_code == 0
    assert seen["call"] == ("MyApp", "android", "emulator-5554")


def test_cli_ui_open_bad_platform():
    r = runner.invoke(mobile_app, ["ui", "open", "X", "-p", "windows"])
    assert r.exit_code == 2
    assert "Unknown --platform" in r.stdout


def test_cli_ui_tap_propagates_failure(monkeypatch):
    monkeypatch.setattr(ad, "available", lambda: True)
    monkeypatch.setattr(ad, "tap", lambda ref: (7, "no such element"))
    r = runner.invoke(mobile_app, ["ui", "tap", "@e9"])
    assert r.exit_code == 7 and "no such element" in r.stdout


def test_cli_ui_replay_missing_script():
    r = runner.invoke(mobile_app, ["ui", "replay", "/does/not/exist.ad"])
    assert r.exit_code == 2 and "not found" in r.stdout


def test_cli_ui_snapshot_records_evidence(monkeypatch):
    monkeypatch.setattr(ad, "available", lambda: True)
    monkeypatch.setattr(ad, "snapshot", lambda **k: (0, "@e1 field 'Email'"))
    r = runner.invoke(mobile_app, ["ui", "snapshot", "-i", "--case", "case_ui1"])
    assert r.exit_code == 0
    from navig_mobile.consent import CaseDir
    from navig_mobile.store import get_store

    cases = get_store().list_cases()
    assert any(k["case_id"] == "case_ui1" for k in cases)
    # the a11y snapshot was hashed into the case's chain-of-custody manifest
    cd = CaseDir.open(next(Path(k["path"]) for k in cases if k["case_id"] == "case_ui1"))
    verify = cd.verify()
    assert verify and all(v["ok"] for v in verify)


def test_help_lists_app_automation_pillar():
    r = runner.invoke(mobile_app, ["--help"])
    assert r.exit_code == 0 and "App Automation" in r.stdout


# ── #2 physical-device consent gate ──────────────────────────────────────────

def test_looks_physical_heuristic():
    from navig_mobile.commands.mobile import _looks_physical

    assert _looks_physical(None) is False           # auto target
    assert _looks_physical("emulator-5554") is False
    assert _looks_physical("localhost:5555") is False
    assert _looks_physical("iPhone 15 Simulator") is False
    assert _looks_physical("A1B2C3D4-1234-5678-9ABC-DEF012345678") is False  # iOS simulator UUID
    assert _looks_physical("39FBC2A1X") is True      # a real Android serial
    assert _looks_physical("00008110-001A2B3C1E88801E") is True  # physical iOS udid (A12+, not a UUID)


def test_ui_open_physical_requires_consent(monkeypatch):
    monkeypatch.setattr(ad, "available", lambda: True)
    called = {"opened": False}
    monkeypatch.setattr(ad, "open_app",
                        lambda *a, **k: (called.__setitem__("opened", True), (0, "ok"))[1])
    # a real serial with no recorded consent → refused (exit 4), open NOT called
    r = runner.invoke(mobile_app, ["ui", "open", "MyApp", "-p", "android", "-u", "39FBC2A1X"])
    assert r.exit_code == 4
    assert called["opened"] is False
    assert "authorization" in r.stdout.lower()


def test_ui_open_physical_allowed_with_consent(monkeypatch):
    monkeypatch.setattr(ad, "available", lambda: True)
    monkeypatch.setattr(ad, "open_app", lambda *a, **k: (0, "opened"))
    from navig_mobile.consent import ConsentGate

    ConsentGate().record(udid="39FBC2A1X", authorization_ref="I own this device")
    r = runner.invoke(mobile_app, ["ui", "open", "MyApp", "-p", "android", "-u", "39FBC2A1X"])
    assert r.exit_code == 0


def test_ui_open_emulator_not_gated(monkeypatch):
    monkeypatch.setattr(ad, "available", lambda: True)
    monkeypatch.setattr(ad, "open_app", lambda *a, **k: (0, "opened"))
    # emulator target needs no consent record
    r = runner.invoke(mobile_app, ["ui", "open", "MyApp", "-p", "android", "-u", "emulator-5554"])
    assert r.exit_code == 0


# ── #3 ui assert (machine-checkable verify primitive) ────────────────────────

def test_ui_assert_present(monkeypatch):
    monkeypatch.setattr(ad, "available", lambda: True)
    monkeypatch.setattr(ad, "snapshot", lambda **k: (0, "@e1 text 'Welcome back'"))
    r = runner.invoke(mobile_app, ["ui", "assert", "Welcome"])
    assert r.exit_code == 0 and "OK" in r.stdout


def test_ui_assert_absent_fails(monkeypatch):
    monkeypatch.setattr(ad, "available", lambda: True)
    monkeypatch.setattr(ad, "snapshot", lambda **k: (0, "@e1 text 'Login'"))
    # -t 0 = single shot: a screen that will never change shouldn't burn the settle window
    r = runner.invoke(mobile_app, ["ui", "assert", "Welcome", "-t", "0"])
    assert r.exit_code == 1 and "FAILED" in r.stdout


def test_ui_assert_gone_inverts(monkeypatch):
    monkeypatch.setattr(ad, "available", lambda: True)
    monkeypatch.setattr(ad, "snapshot", lambda **k: (0, "@e1 text 'Login'"))
    # 'Welcome' is absent → --gone assertion holds
    r = runner.invoke(mobile_app, ["ui", "assert", "Welcome", "--gone"])
    assert r.exit_code == 0


def test_ui_assert_no_session(monkeypatch):
    monkeypatch.setattr(ad, "available", lambda: True)
    monkeypatch.setattr(ad, "snapshot", lambda **k: (1, "no session"))
    r = runner.invoke(mobile_app, ["ui", "assert", "Welcome", "-t", "0"])
    assert r.exit_code == 2 and "open a session" in r.stdout.lower()


def test_ui_assert_json(monkeypatch):
    import json as _json

    monkeypatch.setattr(ad, "available", lambda: True)
    monkeypatch.setattr(ad, "snapshot", lambda **k: (0, "@e1 'Welcome'"))
    r = runner.invoke(mobile_app, ["ui", "assert", "Welcome", "--json"])
    assert r.exit_code == 0
    assert _json.loads(r.stdout)["ok"] is True


# ── assert hardening: exact refs · auto-settle · counts ──────────────────────

def test_ui_occurrences_matches_refs_exactly():
    """REGRESSION: a substring test let '@e20' satisfy a check for '@e2' — the verify
    primitive reported an element present that does not exist."""
    from navig_mobile.commands.mobile import _ui_occurrences

    screen = "@e20 button 'Delete account'\n@e21 text 'Are you sure?'"
    assert _ui_occurrences(screen, "@e2") == 0        # was 2 (false-positive)
    assert _ui_occurrences(screen, "@e20") == 1
    # free text stays a substring count
    assert _ui_occurrences("row row row", "row") == 3
    assert _ui_occurrences("Welcome back", "Welcome") == 1
    assert _ui_occurrences("", "@e1") == 0


def test_ui_assert_ref_no_longer_false_positives(monkeypatch):
    monkeypatch.setattr(ad, "available", lambda: True)
    monkeypatch.setattr(ad, "snapshot", lambda **k: (0, "@e20 button 'Delete'"))
    # @e2 is NOT on screen — only @e20 is. Must fail (it used to pass).
    r = runner.invoke(mobile_app, ["ui", "assert", "@e2", "-t", "0"])
    assert r.exit_code == 1 and "FAILED" in r.stdout
    # the ref that IS there still passes
    r = runner.invoke(mobile_app, ["ui", "assert", "@e20", "-t", "0"])
    assert r.exit_code == 0


def test_ui_assert_settles_on_late_render(monkeypatch):
    """A screen that renders a beat after the action must pass, not flake."""
    monkeypatch.setattr(ad, "available", lambda: True)
    screens = iter([(0, "@e1 text 'Loading…'"), (0, "@e1 text 'Loading…'"),
                    (0, "@e1 text 'Welcome back'")])
    calls = {"n": 0}

    def fake_snapshot(**k):
        calls["n"] += 1
        return next(screens, (0, "@e1 text 'Welcome back'"))

    monkeypatch.setattr(ad, "snapshot", fake_snapshot)
    r = runner.invoke(mobile_app, ["ui", "assert", "Welcome", "-t", "5"])
    assert r.exit_code == 0
    assert calls["n"] >= 3          # it kept looking instead of failing on frame 1


def test_ui_assert_gone_waits_for_disappearance(monkeypatch):
    monkeypatch.setattr(ad, "available", lambda: True)
    screens = iter([(0, "@e1 text 'Saving…'"), (0, "@e1 text 'Done'")])
    monkeypatch.setattr(ad, "snapshot", lambda **k: next(screens, (0, "@e1 text 'Done'")))
    r = runner.invoke(mobile_app, ["ui", "assert", "Saving", "--gone", "-t", "5"])
    assert r.exit_code == 0


def test_ui_assert_single_shot_checks_once(monkeypatch):
    monkeypatch.setattr(ad, "available", lambda: True)
    calls = {"n": 0}

    def fake_snapshot(**k):
        calls["n"] += 1
        return (0, "@e1 text 'Login'")

    monkeypatch.setattr(ad, "snapshot", fake_snapshot)
    r = runner.invoke(mobile_app, ["ui", "assert", "Welcome", "-t", "0"])
    assert r.exit_code == 1 and calls["n"] == 1     # no retry when --timeout 0


def test_ui_assert_unreadable_screen_is_retried_then_reported(monkeypatch):
    """A snapshot that fails mid-transition is transient — retry, and only report
    'no session' (exit 2) if it never becomes readable."""
    monkeypatch.setattr(ad, "available", lambda: True)
    calls = {"n": 0}

    def fake_snapshot(**k):
        calls["n"] += 1
        return (1, "no session")

    monkeypatch.setattr(ad, "snapshot", fake_snapshot)
    r = runner.invoke(mobile_app, ["ui", "assert", "Welcome", "-t", "0.9"])
    assert r.exit_code == 2 and calls["n"] > 1


def test_ui_assert_recovers_when_screen_becomes_readable(monkeypatch):
    monkeypatch.setattr(ad, "available", lambda: True)
    screens = iter([(1, "session starting"), (0, "@e1 text 'Welcome'")])
    monkeypatch.setattr(ad, "snapshot", lambda **k: next(screens, (0, "@e1 text 'Welcome'")))
    r = runner.invoke(mobile_app, ["ui", "assert", "Welcome", "-t", "5"])
    assert r.exit_code == 0          # a transient read failure is not a verdict


def test_ui_assert_count(monkeypatch):
    monkeypatch.setattr(ad, "available", lambda: True)
    monkeypatch.setattr(ad, "snapshot",
                        lambda **k: (0, "@e1 row\n@e2 row\n@e3 row"))
    assert runner.invoke(mobile_app, ["ui", "assert", "row", "--count", "3", "-t", "0"]).exit_code == 0
    r = runner.invoke(mobile_app, ["ui", "assert", "row", "--count", "2", "-t", "0"])
    assert r.exit_code == 1 and "expected ×2" in r.stdout


def test_ui_assert_count_zero_means_absent(monkeypatch):
    monkeypatch.setattr(ad, "available", lambda: True)
    monkeypatch.setattr(ad, "snapshot", lambda **k: (0, "@e1 text 'Login'"))
    assert runner.invoke(
        mobile_app, ["ui", "assert", "Welcome", "--count", "0", "-t", "0"]).exit_code == 0


def test_ui_assert_gone_and_count_conflict(monkeypatch):
    monkeypatch.setattr(ad, "available", lambda: True)
    r = runner.invoke(mobile_app, ["ui", "assert", "x", "--gone", "--count", "2"])
    assert r.exit_code == 2 and "not both" in r.stdout


def test_ui_assert_json_reports_count_and_wait(monkeypatch):
    import json as _json

    monkeypatch.setattr(ad, "available", lambda: True)
    monkeypatch.setattr(ad, "snapshot", lambda **k: (0, "row row"))
    r = runner.invoke(mobile_app, ["ui", "assert", "row", "--count", "2", "-t", "0", "--json"])
    assert r.exit_code == 0
    payload = _json.loads(r.stdout)
    assert payload["ok"] is True and payload["count"] == 2
    assert payload["expected_count"] == 2 and payload["present"] is True
    assert payload["waited_seconds"] >= 0


# ── #1 verified-app Block is well-formed ─────────────────────────────────────

def _plugin_block_path() -> Path:
    """The app-ui-verify BLOCK.md shipped inside the navig_mobile package."""
    import navig_mobile

    return Path(navig_mobile.__file__).parent / "blocks" / "app-ui-verify" / "BLOCK.md"


def test_block_app_ui_verify_valid():
    from navig.blocks.loader import parse_block_file, validate_block

    bf = _plugin_block_path()
    assert bf.exists(), f"block not found at {bf}"
    block = parse_block_file(bf)
    assert block is not None
    problems = validate_block(block)
    assert problems == [], problems
    assert block.id == "app-ui-verify"
    assert block.verify.kind == "command"          # a real machine end-state check
    assert block.verify.argv[:4] == ["navig", "mobile", "ui", "assert"]
    # the verify must carry a settle window — a cold app paints its first screen a
    # beat after launch, and a single-shot assert would report a false failure
    assert "-t" in block.verify.argv
    assert all(isinstance(a, str) for a in block.verify.argv)
    assert {i.key for i in block.inputs} == {"app", "platform", "expect_text"}


def test_app_ui_verify_discoverable_in_package():
    """The block is found when its package `blocks/` dir is scanned — which is what
    core's get_block_dirs() now does via plugin_capability_dirs('blocks')."""
    from navig.blocks.loader import discover_blocks

    blocks_root = _plugin_block_path().parent.parent  # navig_mobile/blocks
    ids = {b.id for b in discover_blocks([blocks_root])}
    assert "app-ui-verify" in ids
