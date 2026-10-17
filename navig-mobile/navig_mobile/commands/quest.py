"""``navig mobile quest …`` — Meta Quest headset ops (Horizon OS over ADB).

connect (USB → wireless ADB) · status · tweaks (render/refresh/foveation/CPU-GPU,
verified by read-back) · perf (live FPS proof from the compositor) · awake ·
mirror/record (scrcpy, one-eye crop) · watch (auto re-apply after a reboot).

Engine logic lives in ``navig_mobile.engine.quest``; this module is CLI only.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated, Optional

import typer

from navig_mobile import config
from navig_mobile.console import Console
from navig_sdk.host import command_name, decode_console_result

CMD = command_name("mobile")
c = Console()

quest_app = typer.Typer(
    name="quest",
    help="🥽 Meta Quest (Horizon OS): wireless ADB, render tweaks with live FPS "
    "proof, mirror/record, keep-awake, auto re-apply after reboot.",
    no_args_is_help=True,
)

SerialOpt = Annotated[Optional[str], typer.Option(
    "--serial", "-s", help="adb serial or ip:port (default: the connected Quest).")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable JSON output.")]

_CFG_TWEAKS = "quest.tweaks"
_CFG_IP = "quest.last_ip"
_CFG_REC = "quest.recordings_dir"
_TASK = "navig quest watch"


# ── helpers ──────────────────────────────────────────────────────────────────

def _table(rows: list[dict], columns: list[str], *, title: str) -> None:
    c.print_rows(columns, [[r.get(k, "") for k in columns] for r in rows], title=title)


def _adb() -> str:
    from navig_mobile.tools import find_binary

    adb = find_binary("adb")
    if not adb:
        c.error(f"adb not found — see `{CMD} doctor` for install.")
        raise typer.Exit(127)
    return str(adb)


def _global_run(adb: str):
    from navig_mobile.engine.quest import make_runner

    return make_runner(adb)


def _pick(adb: str, serial: Optional[str]) -> str:
    """Resolve the target headset, reconnecting to the remembered Wi-Fi IP if needed."""
    from navig_mobile.engine.quest import find_quests

    if serial:
        if ":" in serial:
            _global_run(adb)(["connect", serial])
            config.set(_CFG_IP, serial.split(":")[0])
        return serial
    run = _global_run(adb)
    quests = find_quests(run)
    if not any(q.state == "device" for q in quests):
        last = config.get(_CFG_IP)
        if last:
            run(["connect", f"{last}:5555"])
            quests = find_quests(run)
    ready = [q for q in quests if q.state == "device"]
    if ready:
        if ready[0].is_wifi:  # remember it, so a dropped session reconnects next time
            config.set(_CFG_IP, ready[0].serial.split(":")[0])
        return ready[0].serial
    if any(q.state == "unauthorized" for q in quests):
        c.error("Quest found but not authorized — put the headset on and tap "
                "'Allow USB debugging' (tick 'Always allow').")
        raise typer.Exit(2)
    c.error("No Quest connected. Plug it in by USB-C, then run "
            f"`{CMD} quest connect` to switch it to wireless.")
    raise typer.Exit(1)


def _runner(serial: Optional[str]):
    """Bound runner for the target headset; exits cleanly if it is asleep/offline."""
    from navig_mobile.engine.quest import QuestOffline, make_runner, shell

    adb = _adb()
    target = _pick(adb, serial)
    run = make_runner(adb, target)
    try:
        shell(run, "true")
    except QuestOffline as exc:
        c.error(f"{target} is connected but not responding ({exc}). The headset is "
                f"asleep — put it on (or press the power button), then retry.")
        raise typer.Exit(3) from exc
    return run, target


def _wait_authorized(run, serial: str, seconds: int) -> bool:
    from navig_mobile.engine.quest import parse_adb_devices

    deadline = time.time() + seconds
    while time.time() < deadline:
        for d in parse_adb_devices(run(["devices", "-l"])[1]):
            if d.serial == serial and d.state == "device":
                return True
        if ":" in serial:
            run(["connect", serial])
        time.sleep(3)
    return False


def _print_results(results) -> bool:
    rows = [{"property": r.prop, "wanted": r.wanted or "(default)",
             "read back": r.readback or "(default)", "ok": "✓" if r.ok else "✗"}
            for r in results]
    _table(rows, ["property", "wanted", "read back", "ok"], title="Quest tweaks")
    return all(r.ok for r in results)


def _saved_tweaks():
    from navig_mobile.engine.quest import Tweaks

    raw = config.get(_CFG_TWEAKS)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    return Tweaks.from_dict(raw) if raw else None


def _recordings_dir(out: Optional[Path]) -> Path:
    if out:
        config.set(_CFG_REC, str(out))
        d = out
    else:
        saved = config.get(_CFG_REC)
        d = Path(saved) if saved else config.mobile_dir() / "quest-recordings"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── connect ──────────────────────────────────────────────────────────────────

@quest_app.command("connect")
def cmd_connect(
    serial: SerialOpt = None,
    ip: Annotated[Optional[str], typer.Option(
        "--ip", help="Headset Wi-Fi IP when wireless ADB is already on (no cable needed).")] = None,
    wait: Annotated[int, typer.Option(help="Seconds to wait for the on-headset 'Allow' prompt.")] = 120,
) -> None:
    """Switch a USB-connected Quest to wireless ADB (port 5555) and connect to it.

    Quest builds have no 'pair with code' screen: wireless ADB is enabled once from
    USB and is lost on reboot (`watch` automates redoing it)."""
    from navig_mobile.engine.quest import find_quests, make_runner, wifi_ip

    adb = _adb()
    run = _global_run(adb)
    if ip:
        target = f"{ip}:5555"
        run(["connect", target])
        if not _wait_authorized(run, target, min(wait, 15)):
            c.error(f"{target} did not answer — wireless ADB is off (reboot?). "
                    "Plug in by USB and run `connect` without --ip.")
            raise typer.Exit(1)
        config.set(_CFG_IP, ip)
        c.success(f"Connected to {target} and remembered it.")
        return
    quests = find_quests(run)
    usb = [q for q in quests if not q.is_wifi]
    if serial is None:
        wifi_ready = [q for q in quests if q.is_wifi and q.state == "device"]
        if wifi_ready and not usb:
            c.success(f"Already connected wirelessly: {wifi_ready[0].serial}")
            return
        if not usb:
            last = config.get(_CFG_IP)
            if last and run(["connect", f"{last}:5555"])[0] == 0 and _wait_authorized(
                    run, f"{last}:5555", 5):
                c.success(f"Reconnected to {last}:5555")
                return
            c.error("No Quest on USB. Plug it in with a data USB-C cable and accept "
                    "'Allow USB debugging' in the headset.")
            raise typer.Exit(1)
        serial = usb[0].serial
    if not _wait_authorized(run, serial, wait):
        c.error("Not authorized — tap 'Allow USB debugging' inside the headset.")
        raise typer.Exit(2)
    dev = make_runner(adb, serial)
    ip = wifi_ip(dev)
    if not ip:
        c.error("Headset has no Wi-Fi address — connect it to Wi-Fi first.")
        raise typer.Exit(1)
    c.step(f"Enabling wireless ADB on {ip}:5555 …")
    dev(["tcpip", "5555"])
    time.sleep(4)
    target = f"{ip}:5555"
    run(["connect", target])
    c.step("If the headset asks again, put it on and tap Allow.")
    if not _wait_authorized(run, target, wait):
        c.error(f"Wireless link {target} not authorized within {wait}s.")
        raise typer.Exit(2)
    config.set(_CFG_IP, ip)
    c.success(f"Wireless ADB ready: {target} — you can unplug the cable.")


# ── status ───────────────────────────────────────────────────────────────────

@quest_app.command("status")
def cmd_status(serial: SerialOpt = None, json_out: JsonOpt = False) -> None:
    """Model, Horizon build, battery, sleep state, storage, active tweaks."""
    from dataclasses import asdict

    from navig_mobile.engine.quest import status

    run, target = _runner(serial)
    st = status(run)
    if json_out:
        typer.echo(json.dumps({"serial": target, **asdict(st)}, indent=2))
        return
    rows = [
        {"field": "Device", "value": f"{st.model} ({target})"},
        {"field": "Android / Horizon build", "value": f"{st.android} / {st.horizon_build}"},
        {"field": "Security patch", "value": st.security_patch},
        {"field": "Battery", "value": f"{st.battery}%" if st.battery is not None else "?"},
        {"field": "State", "value": {True: "awake", False: "asleep", None: "?"}[st.awake]},
        {"field": "Storage", "value": st.storage},
        {"field": "Wi-Fi IP", "value": st.wifi_ip or "—"},
    ]
    rows += [{"field": p.replace("debug.oculus.", "tweak: "), "value": v or "(default)"}
             for p, v in st.props.items()]
    _table(rows, ["field", "value"], title="Quest")


# ── tweaks ───────────────────────────────────────────────────────────────────

@quest_app.command("tweaks")
def cmd_tweaks(
    profile: Annotated[Optional[str], typer.Argument(
        help="quality (3072×3216 · 120 Hz · fov 2 · CPU/GPU 4) | balanced (2560×2688 · 90 Hz · 3/3). "
        "Omit to re-apply the saved set.")] = None,
    texture: Annotated[Optional[str], typer.Option(help="Per-eye render size WxH, e.g. 3072x3216.")] = None,
    hz: Annotated[Optional[int], typer.Option(help="Refresh rate (72–120 typical).")] = None,
    foveation: Annotated[Optional[int], typer.Option(help="Foveation level 0 (off) – 4 (high).")] = None,
    dynamic: Annotated[Optional[bool], typer.Option("--dynamic/--no-dynamic", help="Dynamic foveation.")] = None,
    cpu: Annotated[Optional[int], typer.Option(help="CPU level.")] = None,
    gpu: Annotated[Optional[int], typer.Option(help="GPU level.")] = None,
    reset: Annotated[bool, typer.Option("--reset", help="Clear everything back to headset defaults.")] = False,
    save: Annotated[bool, typer.Option("--save/--no-save", help="Remember this set for `watch`.")] = True,
    serial: SerialOpt = None,
) -> None:
    """Apply render/perf tweaks and verify each by read-back. Reboot clears them;
    restart the game for them to take effect. Prove they reached the game with `perf`."""
    from dataclasses import replace

    from navig_mobile.engine.quest import PROFILES, apply_tweaks, reset_tweaks

    run, target = _runner(serial)
    if reset:
        ok = _print_results(reset_tweaks(run))
        if save:
            config.set(_CFG_TWEAKS, {})
        (c.success if ok else c.warning)("Tweaks cleared — headset back to its defaults.")
        return
    if profile:
        if profile not in PROFILES:
            c.error(f"Unknown profile '{profile}'. Choose: {', '.join(PROFILES)}.")
            raise typer.Exit(2)
        tw = replace(PROFILES[profile])
    else:
        tw = _saved_tweaks() or replace(PROFILES["quality"])
    if texture:
        try:
            w, h = (int(x) for x in texture.lower().split("x"))
        except ValueError as exc:
            c.error("--texture must look like 3072x3216.")
            raise typer.Exit(2) from exc
        tw.texture_width, tw.texture_height = w, h
    for attr, val in (("refresh_rate", hz), ("foveation_level", foveation),
                      ("cpu_level", cpu), ("gpu_level", gpu)):
        if val is not None:
            setattr(tw, attr, val)
    if dynamic is not None:
        tw.foveation_dynamic = int(dynamic)
    ok = _print_results(apply_tweaks(run, tw))
    if save:
        config.set(_CFG_TWEAKS, tw.to_dict())
    if ok:
        c.success(f"Applied to {target}. Restart the game, then `{CMD} quest perf` to prove it.")
    else:
        c.warning("Some values did not read back — this headset build may block them.")
        raise typer.Exit(1)


# ── perf ─────────────────────────────────────────────────────────────────────

@quest_app.command("perf")
def cmd_perf(
    last: Annotated[int, typer.Option(help="How many recent samples to show.")] = 5,
    follow: Annotated[int, typer.Option(help="Keep sampling for N seconds (0 = once).")] = 0,
    serial: SerialOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Live FPS / foveation / CPU-GPU / temperature from the compositor — the proof
    that tweaks reached a running game. Needs a VR app open (headset worn)."""
    from dataclasses import asdict

    from navig_mobile.engine.quest import perf

    run, _ = _runner(serial)
    end = time.time() + follow
    while True:
        samples = perf(run, last)
        if json_out:
            typer.echo(json.dumps([asdict(s) for s in samples], indent=2))
        elif not samples:
            c.warning("No VrApi samples — open a VR game with the headset on, then retry.")
        else:
            _table([{"time": s.time, "fps": f"{s.fps}/{s.target_fps}",
                           "foveation": s.foveation, "cpu/gpu": s.cpu_gpu, "temp": s.temp}
                          for s in samples],
                         ["time", "fps", "foveation", "cpu/gpu", "temp"], title="Quest perf")
        if time.time() >= end:
            break
        time.sleep(5)


# ── awake ────────────────────────────────────────────────────────────────────

@quest_app.command("awake")
def cmd_awake(
    state: Annotated[str, typer.Argument(help="on = act as if worn · off = back to the real sensor")],
    serial: SerialOpt = None,
) -> None:
    """Keep the headset awake without wearing it (for mirroring, setup, demos)."""
    from navig_mobile.engine.quest import set_awake_override

    if state not in ("on", "off"):
        c.error("Use `on` or `off`.")
        raise typer.Exit(2)
    run, _ = _runner(serial)
    set_awake_override(run, state == "on")
    c.success("Headset held awake (remember `awake off` — it drains battery)."
              if state == "on" else "Proximity sensor back in control.")


# ── mirror / record ──────────────────────────────────────────────────────────

@quest_app.command("mirror")
def cmd_mirror(
    record: Annotated[bool, typer.Option("--record", help="Also save an MP4.")] = False,
    both: Annotated[bool, typer.Option("--both", help="Show both eyes (default: left eye only).")] = False,
    keep_awake: Annotated[bool, typer.Option("--keep-awake", help="Stream while nobody wears it.")] = False,
    seconds: Annotated[Optional[int], typer.Option(help="Stop automatically after N seconds.")] = None,
    no_window: Annotated[bool, typer.Option("--no-window", help="Record only, no window.")] = False,
    out: Annotated[Optional[Path], typer.Option("--out", "-o", help="Recordings folder (remembered).")] = None,
    serial: SerialOpt = None,
) -> None:
    """Mirror the headset on this PC (scrcpy) — clean one-eye view, optional MP4."""
    from navig_mobile.engine.quest import scrcpy_args, set_awake_override
    from navig_mobile.tools import find_binary

    scrcpy = find_binary("scrcpy")
    if not scrcpy:
        c.error(f"scrcpy not found — see `{CMD} doctor` for install.")
        raise typer.Exit(127)
    run, target = _runner(serial)
    if no_window and not record:
        c.error("--no-window only makes sense with --record.")
        raise typer.Exit(2)
    rec_path = None
    if record:
        rec_path = _recordings_dir(out) / f"quest-{datetime.now():%Y%m%d-%H%M%S}.mp4"
    if keep_awake:
        set_awake_override(run, True)
    argv = [str(scrcpy), *scrcpy_args(target, both_eyes=both,
                                       record=str(rec_path) if rec_path else None,
                                       window=not no_window, time_limit=seconds)]
    c.step("Mirroring … close the window (or Ctrl-C) to stop.")
    try:
        subprocess.run(argv)
    except KeyboardInterrupt:
        pass
    finally:
        if keep_awake:
            set_awake_override(run, False)
    if rec_path:
        size = rec_path.stat().st_size if rec_path.exists() else 0
        if size < 64 * 1024:
            c.warning(f"Recording is empty ({size} B) — the headset was probably asleep. "
                      "Wear it, or add --keep-awake.")
            raise typer.Exit(1)
        c.success(f"Saved {rec_path} ({size / 1048576:.1f} MB)")


# ── watch (auto re-apply after reboot) ──────────────────────────────────────

def _watch_once(adb: str) -> str:
    from navig_mobile.engine.quest import find_quests, make_runner, wifi_ip
    from navig_mobile.engine.quest import apply_tweaks, tweaks_missing

    run = make_runner(adb)
    quests = find_quests(run)
    if not quests:
        last = config.get(_CFG_IP)
        if last:
            run(["connect", f"{last}:5555"])
            quests = find_quests(run)
    ready = [q for q in quests if q.state == "device"]
    if not ready:
        return "waiting"
    notes = []
    if not any(q.is_wifi for q in ready):
        usb = ready[0].serial
        dev = make_runner(adb, usb)
        ip = wifi_ip(dev)
        if ip:
            dev(["tcpip", "5555"])
            time.sleep(4)
            run(["connect", f"{ip}:5555"])
            config.set(_CFG_IP, ip)
            notes.append(f"wireless ADB re-enabled on {ip}:5555")
    target = make_runner(adb, ready[0].serial)
    tw = _saved_tweaks()
    if tw and tw.props() and tweaks_missing(target, tw):
        res = apply_tweaks(target, tw)
        notes.append("tweaks re-applied" + ("" if all(r.ok for r in res) else " (some rejected)"))
    return "; ".join(notes) or "ok"


def _watch_target() -> tuple[str, str]:
    """Program + arguments that start `watch` hidden at sign-in."""
    exe = shutil.which("navig")
    inner = f"& '{exe}' mobile quest watch" if exe else \
        f"& '{sys.executable}' -m navig_mobile.cli quest watch"
    return "powershell.exe", f'-NoProfile -WindowStyle Hidden -Command "{inner}"'


def _startup_dir() -> Path:
    """Per-user Startup folder — a shortcut there runs at sign-in with no elevation."""
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _lnk_path() -> Path:
    return _startup_dir() / f"{_TASK}.lnk"


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _install_startup() -> tuple[bool, str]:
    """Write the Startup shortcut (Task Scheduler refuses logon tasks to a normal shell).

    Returns ``(ok, detail)``; ``detail`` is the shortcut path or the failure reason.
    """
    lnk = _lnk_path()
    prog, args = _watch_target()
    script = (
        "$s = New-Object -ComObject WScript.Shell\n"
        f"$l = $s.CreateShortcut({_ps_quote(str(lnk))})\n"
        f"$l.TargetPath = {_ps_quote(prog)}\n"
        f"$l.Arguments = {_ps_quote(args)}\n"
        "$l.WindowStyle = 7\n"
        "$l.Save()\n"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    p = decode_console_result(subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True))
    if p.returncode != 0 or not lnk.exists():
        return False, (p.stderr or p.stdout).strip() or f"{lnk} was not written"
    return True, str(lnk)


@quest_app.command("watch")
def cmd_watch(
    interval: Annotated[int, typer.Option(help="Seconds between checks.")] = 30,
    once: Annotated[bool, typer.Option("--once", help="Check one time and exit.")] = False,
    install: Annotated[bool, typer.Option("--install", help="Run automatically at Windows sign-in.")] = False,
    uninstall: Annotated[bool, typer.Option("--uninstall", help="Remove the sign-in hook.")] = False,
) -> None:
    """Re-apply your saved tweaks whenever the headset comes back.

    A reboot wipes both the tweaks and wireless ADB. Over Wi-Fi this re-applies as
    soon as the headset is reachable; after a full reboot plug the USB cable in
    once — `watch` then re-enables wireless ADB and the tweaks by itself."""
    if install or uninstall:
        if not sys.platform.startswith("win"):
            c.error("--install/--uninstall run the hook at Windows sign-in.")
            raise typer.Exit(2)
        if uninstall:
            removed: list[str] = []
            rc = decode_console_result(subprocess.run(
                ["schtasks", "/Delete", "/TN", _TASK, "/F"], capture_output=True)).returncode
            if rc == 0:
                removed.append("sign-in task")
            lnk = _lnk_path()
            if lnk.exists():
                lnk.unlink()
                removed.append("Startup shortcut")
            (c.success if removed else c.warning)(
                "Removed " + " and ".join(removed) + "." if removed
                else "Nothing to remove — no sign-in task or Startup shortcut.")
            return
        prog, args = _watch_target()
        p = decode_console_result(subprocess.run(
            ["schtasks", "/Create", "/TN", _TASK, "/TR", f"{prog} {args}", "/SC",
             "ONLOGON", "/RL", "LIMITED", "/F"], capture_output=True))
        if p.returncode == 0:
            stale = _lnk_path()          # exactly one hook: the task supersedes a shortcut
            if stale.exists():
                stale.unlink()
                c.info("Dropped the Startup shortcut — the scheduled task covers it now.")
            c.success(f"'{_TASK}' will start at every sign-in. Remove with `--uninstall`.")
            return
        err = (p.stderr or p.stdout).strip()
        ok, detail = _install_startup()
        if not ok:
            c.error(f"Could not create the sign-in hook: {err}\nStartup fallback: {detail}")
            raise typer.Exit(1)
        c.success("Task Scheduler refuses a logon task to a non-elevated shell "
                  f"({err}), so a Startup shortcut was installed instead: {detail}\n"
                  "It starts `watch` at every sign-in. Remove with `--uninstall`.")
        return
    adb = _adb()
    if not _saved_tweaks():
        c.warning(f"No saved tweaks yet — run `{CMD} quest tweaks quality` once first.")
    last = ""
    from navig_mobile.engine.quest import QuestOffline

    while True:
        try:
            result = _watch_once(adb)
        except QuestOffline:
            result = "waiting (headset asleep)"
        if result != last:
            c.info(f"[{datetime.now():%H:%M:%S}] {result}")
            last = result
        if once:
            return
        time.sleep(interval)
