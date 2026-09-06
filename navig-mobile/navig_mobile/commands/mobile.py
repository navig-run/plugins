"""navig-mobile CLI — ``navig mobile …`` (+ ``navig android`` / ``navig ios``).

The unified ``navig mobile <verb>`` auto-detects the single connected device and
dispatches; platform-only ops live under ``navig mobile android|ios …`` and are
also exposed as the alias verbs ``navig android`` / ``navig ios`` (the same
sub-Typers). Verbs are grouped into pillars via ``rich_help_panel``.

Engine imports are lazy (inside command bodies) so ``navig help`` stays fast and
importing this module pulls no third-party library.
"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Annotated, NamedTuple, Optional

import typer

from navig.core.proc_text import decode_console_result
from navig_mobile.console import Console

c = Console()

mobile_app = typer.Typer(
    name="mobile",
    help="📱 Mobile: Android + iOS device ops — connect · info · apps · files · "
    "screenshots · backups · forensics · spyware scan · root/jailbreak assist.",
    no_args_is_help=True,
)
android_app = typer.Typer(
    name="android",
    help="🤖 Android-only ops (logcat, connect, fastboot, sideload…).",
    no_args_is_help=True,
)
ios_app = typer.Typer(
    name="ios",
    help="🍎 iOS-only ops (crash logs, syslog, DFU/recovery, instruments…).",
    no_args_is_help=True,
)
ui_app = typer.Typer(
    name="ui",
    help="🖐️ App UI automation & verification — drive/verify app UIs (a11y "
    "snapshots, tap/fill, evidence capture, replay) via agent-device.",
    no_args_is_help=True,
)
mobile_app.add_typer(android_app, name="android", rich_help_panel="Platform-specific")
mobile_app.add_typer(ios_app, name="ios", rich_help_panel="Platform-specific")
mobile_app.add_typer(ui_app, name="ui", rich_help_panel="App Automation")

# ── reusable options ─────────────────────────────────────────────────────────
UdidOpt = Annotated[Optional[str], typer.Option(
    "--udid", "-u", help="Target device UDID/serial (auto when only one is connected).")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable JSON output.")]
YesOpt = Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")]
SystemOpt = Annotated[bool, typer.Option("--system", help="Include system apps.")]
PlatformOpt = Annotated[Optional[str], typer.Option(
    "--platform", "-p", help="Target platform: ios | android | web | tvos | macos | linux.")]
CaseOpt = Annotated[Optional[str], typer.Option(
    "--case", help="Record produced evidence into this case dir (created if new).")]


# ── helpers ──────────────────────────────────────────────────────────────────

def _manager():
    from navig_mobile.engine.base import DeviceManager

    return DeviceManager()


def _resolve(udid: Optional[str] = None, platform: Optional[str] = None):
    """Resolve the addressed device or exit with a friendly message."""
    from navig_mobile.engine.base import (
        AmbiguousDeviceError,
        DeviceError,
        NoDeviceError,
        Platform,
        PlatformUnavailableError,
    )

    plat = None
    if platform in ("android", "ios"):
        plat = Platform(platform)
    try:
        return _manager().resolve(udid=udid, platform=plat)
    except PlatformUnavailableError as exc:
        c.error(str(exc))
        raise typer.Exit(3) from exc
    except AmbiguousDeviceError as exc:
        c.error(str(exc))
        raise typer.Exit(2) from exc
    except NoDeviceError as exc:
        c.error(str(exc))
        raise typer.Exit(1) from exc
    except DeviceError as exc:
        c.error(str(exc))
        raise typer.Exit(1) from exc


def _record_device(info) -> None:
    try:
        from navig_mobile.store import get_store

        get_store().record_device(
            udid=info.udid, platform=info.platform.value, name=info.name,
            model=info.model, os_version=info.os_version, serial=info.serial,
            extra=info.extra,
        )
    except Exception:
        pass  # inventory is best-effort; never block a device op


def _emit_json(obj) -> None:
    import json

    typer.echo(json.dumps(obj, indent=2, default=str))


def _staged(feature: str, stage: str, hint: str = "") -> None:
    """Honest placeholder for a pillar that ships in a later stage."""
    c.warning(f"{feature} lands in {stage}.")
    if hint:
        c.dim("  " + hint)
    c.dim("  Available now: devices · doctor · info · apps · fs · screenshot · backup. "
          "See `navig mobile doctor` and CHANGELOG.md.")
    raise typer.Exit(0)


def _human_bytes(n: Optional[int]) -> str:
    if not n:
        return "—"
    step = 1024.0
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < step:
            return f"{val:.0f}{unit}" if unit == "B" else f"{val:.1f}{unit}"
        val /= step
    return f"{val:.1f}PB"


# ═════════════════════════ Device & Connect ═════════════════════════════════

@mobile_app.command("devices", rich_help_panel="Device & Connect")
def cmd_devices(json_out: JsonOpt = False) -> None:
    """List every connected/paired Android + iOS device."""
    from navig_mobile import tools

    infos = _manager().list_devices()
    for i in infos:
        _record_device(i)
    if json_out:
        _emit_json([i.to_dict() for i in infos])
        return
    if not infos:
        c.warning("No devices connected.")
        tc = tools.detect()
        if not tc.android_ready:
            c.dim('  Android: pip install "navig-mobile[android]" + enable USB debugging.')
        if not tc.ios_ready:
            c.dim('  iOS: pip install "navig-mobile[ios]" + tap Trust on the device.')
        c.dim("  Then re-run `navig mobile devices`, or `navig mobile doctor` for a full check.")
        raise typer.Exit(0)
    rows = [[i.udid, i.platform.value, i.name or "—", i.model or "—",
             i.os_version or "—", i.connection.value] for i in infos]
    c.print_rows(["UDID", "Platform", "Name", "Model", "OS", "Conn"], rows,
                 title=f"{len(infos)} device(s)")
    c.dim("Next: navig mobile info -u <udid> · navig mobile apps list -u <udid>")


@mobile_app.command("doctor", rich_help_panel="Device & Connect")
def cmd_doctor(json_out: JsonOpt = False) -> None:
    """Toolchain health — what's installed for Android/iOS ops and how to get the rest."""
    from navig_mobile import tools

    tc = tools.detect()
    if json_out:
        _emit_json([t.__dict__ for t in tc.tools])
        return
    rows = []
    for t in tc.tools:
        status = c.styled("● found", "green") if t.found else c.styled("○ missing", "yellow")
        detail = t.detail or ("" if t.found else (t.install_hint or t.note))
        rows.append([t.label, t.kind, status, detail])
    c.print_rows(["Tool", "Kind", "Status", "Detail / install"], rows,
                 title="navig mobile — toolchain")
    c.plain()
    c.info(f"Android engine: {'ready' if tc.android_ready else 'not installed'}  ·  "
           f"iOS engine: {'ready' if tc.ios_ready else 'not installed'}")
    if not (tc.android_ready and tc.ios_ready):
        c.dim('  Install both engines: pip install "navig-mobile[all]"')


@mobile_app.command("connect", rich_help_panel="Device & Connect")
def cmd_connect(
    address: Annotated[str, typer.Argument(help="host:port (Android adb over TCP) — e.g. 192.168.1.20:5555")],
) -> None:
    """Connect to an Android device over TCP/IP (adb connect)."""
    import shutil
    import subprocess

    adb = shutil.which("adb")
    if not adb:
        c.error("The `adb` binary is required — see `navig mobile doctor`.")
        raise typer.Exit(127)
    proc = decode_console_result(subprocess.run([adb, "connect", address], capture_output=True, timeout=20))
    out = (proc.stdout or proc.stderr or "").strip()
    (c.success if "connected" in out.lower() else c.warning)(out or "no output")


@mobile_app.command("pair", rich_help_panel="Device & Connect")
def cmd_pair(
    address: Annotated[Optional[str], typer.Argument(help="host:port for Android wireless pairing")] = None,
    code: Annotated[Optional[str], typer.Option("--code", help="6-digit pairing code (adb pair).")] = None,
) -> None:
    """Pair a device — Android wireless (`adb pair host:port`), else guidance."""
    if address:
        import shutil
        import subprocess

        adb = shutil.which("adb")
        if not adb:
            c.error("`adb` binary required for wireless pairing — see `navig mobile doctor`.")
            raise typer.Exit(127)
        if code:
            proc = decode_console_result(subprocess.run([adb, "pair", address, code],
                                  capture_output=True, timeout=60))
            out = (proc.stdout or proc.stderr or "").strip()
            (c.success if proc.returncode == 0 else c.warning)(out or "no output")
            raise typer.Exit(proc.returncode)
        c.step(f"adb pair {address} — enter the 6-digit code when prompted …")
        raise typer.Exit(subprocess.run([adb, "pair", address]).returncode)
    # no address → guidance (pairing is otherwise a physical/UI action)
    c.info("Pair a device:")
    c.dim("  • Android wireless: enable 'Wireless debugging' → 'Pair with code', then "
          "`navig mobile pair <host:port> --code <code>`.")
    c.dim("  • Android USB: accept the USB-debugging RSA prompt on the device.")
    c.dim("  • iOS: unlock the device and tap 'Trust'.")
    c.dim("  Available now: devices · doctor · info · apps · fs · screenshot · backup.")


@mobile_app.command("watch", rich_help_panel="Device & Connect")
def cmd_watch(
    interval: Annotated[float, typer.Option("--interval", help="Poll seconds.")] = 2.0,
) -> None:
    """Watch for device arrival/removal until interrupted (Ctrl-C)."""
    import time

    c.info("Watching for device arrival/removal — Ctrl-C to stop …")
    seen: dict[str, object] = {}
    try:
        while True:
            infos = {i.udid: i for i in _manager().list_devices()}
            for udid, i in infos.items():
                if udid not in seen:
                    c.success(f"+ connected  {udid}  ({i.platform.value}/{i.name or i.model or '—'})")
            for udid in list(seen):
                if udid not in infos:
                    c.warning(f"- removed    {udid}")
            seen = infos
            time.sleep(max(0.5, interval))
    except KeyboardInterrupt:
        c.dim("stopped.")


# ═════════════════════════ Info & Diagnostics ═══════════════════════════════

@mobile_app.command("info", rich_help_panel="Info & Diagnostics")
def cmd_info(udid: UdidOpt = None, json_out: JsonOpt = False) -> None:
    """Unified device info (auto-detects the connected platform)."""
    dev = _resolve(udid)
    from navig_mobile.engine.base import DeviceError

    try:
        info = dev.info()
    except DeviceError as exc:
        c.error(str(exc))
        raise typer.Exit(1) from exc
    _record_device(info)
    if json_out:
        _emit_json(info.to_dict())
        return
    rows = [
        ["Name", info.name or "—"],
        ["Platform", info.platform.value],
        ["Model", info.model or "—"],
        ["OS version", info.os_version or "—"],
        ["UDID", info.udid],
        ["Serial", info.serial or "—"],
        ["Battery", f"{info.battery}%" if info.battery is not None else "—"],
        ["Storage", f"{_human_bytes(info.storage_free)} free / {_human_bytes(info.storage_total)}"],
        ["Connection", info.connection.value],
        ["Root/JB", _tri(info.rooted_or_jailbroken)],
        ["Dev mode", _tri(info.developer_mode)],
    ]
    c.print_rows(["Field", "Value"], rows, title=f"{info.name or info.udid}")


@mobile_app.command("battery", rich_help_panel="Info & Diagnostics")
def cmd_battery(udid: UdidOpt = None) -> None:
    """Battery level."""
    info = _resolve(udid).info()
    if info.battery is None:
        c.warning("Battery level unavailable for this device.")
    else:
        c.info(f"Battery: {info.battery}%")


@mobile_app.command("storage", rich_help_panel="Info & Diagnostics")
def cmd_storage(udid: UdidOpt = None) -> None:
    """Storage usage."""
    info = _resolve(udid).info()
    c.info(f"Storage: {_human_bytes(info.storage_free)} free of {_human_bytes(info.storage_total)}")


@mobile_app.command("logs", rich_help_panel="Info & Diagnostics")
def cmd_logs(
    udid: UdidOpt = None,
    lines: Annotated[int, typer.Option("--lines", "-n", help="Recent lines (Android).")] = 200,
) -> None:
    """Device log — recent logcat (Android) or live syslog (iOS)."""
    from navig_mobile.engine.base import DeviceError, Platform

    dev = _resolve(udid)
    if dev.platform is Platform.ANDROID:
        try:
            out = dev._raw.shell(f"logcat -d -t {int(lines)}")  # type: ignore[attr-defined]
        except DeviceError as exc:
            c.error(str(exc))
            raise typer.Exit(1) from exc
        except Exception as exc:
            c.error(f"logcat failed: {exc}")
            raise typer.Exit(1) from exc
        typer.echo(out)
    else:
        from navig_mobile.engine.devtools import ios_dev

        c.step("Streaming iOS syslog — Ctrl-C to stop …")
        raise typer.Exit(ios_dev.syslog_live(dev.udid))


# ═════════════════════════ Apps ═════════════════════════════════════════════

@mobile_app.command("apps", rich_help_panel="Apps")
def cmd_apps(
    action: Annotated[str, typer.Argument(help="list | install | uninstall | info | extract")] = "list",
    target: Annotated[Optional[str], typer.Argument(help="app id / .apk / .ipa (for install/uninstall/info/extract)")] = None,
    udid: UdidOpt = None,
    system: SystemOpt = False,
    out: Annotated[Optional[Path], typer.Option("--out", "-o", help="Output path for `extract`.")] = None,
    json_out: JsonOpt = False,
) -> None:
    """Installed apps: list · install <ipa/apk> · uninstall <id> · info <id> · extract <id>."""
    from navig_mobile.engine.base import DeviceError

    dev = _resolve(udid)
    try:
        if action == "list":
            apps = dev.apps(system=system)
            if json_out:
                _emit_json([a.to_dict() for a in apps])
                return
            rows = [[a.app_id, a.name or "—", a.version or "—",
                     "sys" if a.is_system else "user"] for a in apps]
            c.print_rows(["App ID", "Name", "Version", "Type"], rows,
                         title=f"{len(apps)} app(s)")
        elif action == "install":
            _need(target, "install requires a path to an .apk/.ipa")
            c.step(f"Installing {target} …")
            dev.install_app(target)
            c.success("Installed.")
        elif action == "uninstall":
            _need(target, "uninstall requires an app id")
            dev.uninstall_app(target)
            c.success(f"Uninstalled {target}.")
        elif action == "info":
            _need(target, "info requires an app id")
            ai = dev.app_info(target)
            if not ai:
                c.warning(f"App {target!r} not found.")
                raise typer.Exit(1)
            if json_out:
                _emit_json(ai.to_dict())
            else:
                c.print_rows(["Field", "Value"], [
                    ["App ID", ai.app_id], ["Name", ai.name or "—"],
                    ["Version", ai.version or "—"], ["Path", ai.path or "—"],
                    ["Type", "system" if ai.is_system else "user"],
                ], title=ai.name or ai.app_id)
        elif action == "extract":
            _need(target, "extract requires an app id")
            dest = str(out or Path.cwd())
            c.step(f"Extracting {target} → {dest} …")
            saved = dev.extract_app(target, dest)
            c.success(f"Saved: {saved}")
        else:
            c.error(f"Unknown apps action {action!r}. Use list/install/uninstall/info/extract.")
            raise typer.Exit(2)
    except DeviceError as exc:
        c.error(str(exc))
        raise typer.Exit(1) from exc


# ═════════════════════════ Files & Pull ═════════════════════════════════════

@mobile_app.command("fs", rich_help_panel="Files & Pull")
def cmd_fs(
    action: Annotated[str, typer.Argument(help="ls | pull | push")],
    src: Annotated[Optional[str], typer.Argument(help="remote path (ls/pull) or local path (push)")] = None,
    dst: Annotated[Optional[str], typer.Argument(help="local dest (pull) or remote dest (push)")] = None,
    udid: UdidOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Device filesystem: ls <remote> · pull <remote> <local> · push <local> <remote>."""
    from navig_mobile.engine.base import DeviceError

    dev = _resolve(udid)
    try:
        if action == "ls":
            entries = dev.ls(src or "/")
            if json_out:
                _emit_json(entries)
                return
            rows = [[e.get("name", ""), "dir" if e.get("is_dir") else "file",
                     _human_bytes(int(e["size"])) if e.get("size") else "—"] for e in entries]
            c.print_rows(["Name", "Type", "Size"], rows, title=src or "/")
        elif action == "pull":
            _need(src, "pull requires a remote path")
            saved = dev.pull(src, dst or str(Path.cwd()))
            c.success(f"Pulled → {saved}")
        elif action == "push":
            _need(src, "push requires a local path")
            _need(dst, "push requires a remote destination")
            dev.push(src, dst)
            c.success(f"Pushed → {dst}")
        else:
            c.error(f"Unknown fs action {action!r}. Use ls/pull/push.")
            raise typer.Exit(2)
    except DeviceError as exc:
        c.error(str(exc))
        raise typer.Exit(1) from exc


@mobile_app.command("media", rich_help_panel="Files & Pull")
def cmd_media(
    udid: UdidOpt = None,
    out: Annotated[Optional[Path], typer.Option("--out", "-o", help="Destination dir.")] = None,
) -> None:
    """Pull the camera roll / DCIM off the device (consent-gated)."""
    import shutil
    import subprocess

    from navig_mobile.engine.base import DeviceError, Platform

    dev = _resolve(udid)
    _require_consent(dev.udid, "media pull")
    dest = Path(out or (Path.cwd() / f"media-{dev.udid.replace(':', '_')}"))
    dest.mkdir(parents=True, exist_ok=True)
    try:
        if dev.platform is Platform.IOS:
            c.step("Pulling iOS DCIM via AFC …")
            saved = dev.pull("/DCIM", str(dest))
        else:
            adb = shutil.which("adb")
            if not adb:
                c.error("`adb` binary required for Android media pull — see `navig mobile doctor`.")
                raise typer.Exit(127)
            c.step("Pulling Android /sdcard/DCIM …")
            rc = subprocess.run([adb, "-s", dev.udid, "pull", "/sdcard/DCIM", str(dest)],
                                timeout=1800).returncode
            if rc != 0:
                c.error(f"adb pull failed (exit {rc}).")
                raise typer.Exit(1)
            saved = str(dest)
    except DeviceError as exc:
        c.error(str(exc))
        raise typer.Exit(1) from exc
    c.success(f"Media pulled → {saved}")


# ═════════════════════════ Backup & Restore ═════════════════════════════════

@mobile_app.command("backup", rich_help_panel="Backup & Restore")
def cmd_backup(
    action: Annotated[str, typer.Argument(help="create | list")] = "create",
    udid: UdidOpt = None,
    out: Annotated[Optional[Path], typer.Option("--out", "-o", help="Backup destination dir.")] = None,
    encrypted: Annotated[bool, typer.Option("--encrypted", help="Request an encrypted backup (iOS).")] = False,
    json_out: JsonOpt = False,
) -> None:
    """Device backup: create a full backup, or list past backups."""
    from navig_mobile import config
    from navig_mobile.engine.base import DeviceError
    from navig_mobile.store import get_store

    if action == "list":
        backups = get_store().list_backups(udid)
        if json_out:
            _emit_json(backups)
            return
        if not backups:
            c.warning("No backups recorded yet. Run `navig mobile backup create`.")
            return
        rows = [[b["udid"], b.get("platform") or "—", _human_bytes(b.get("bytes")),
                 "yes" if b.get("encrypted") else "no", b.get("created", "")[:19],
                 b.get("path", "")] for b in backups]
        c.print_rows(["UDID", "Platform", "Size", "Enc", "Created", "Path"], rows,
                     title=f"{len(backups)} backup(s)")
        return

    if action != "create":
        c.error(f"Unknown backup action {action!r}. Use create/list.")
        raise typer.Exit(2)

    dev = _resolve(udid)
    info = dev.info()
    dest_base = out or (config.mobile_dir() / "backups")
    dest = Path(dest_base) / f"{info.udid.replace(':', '_')}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    c.info(f"Backing up {info.name or info.udid} ({info.platform.value}) → {dest}")
    c.dim("  This can take several minutes; large backups need free disk space.")

    def _progress(done: int, total: int, note: str) -> None:
        if note:
            c.dim("  " + note)

    try:
        path = _run_with_spinner(
            lambda: dev.backup(str(dest), encrypted=encrypted, progress=_progress),
            "backing up…")
    except DeviceError as exc:
        c.error(str(exc))
        raise typer.Exit(1) from exc

    size, sha = _measure(Path(path))
    try:
        get_store().record_backup(udid=info.udid, platform=info.platform.value,
                                  path=str(path), encrypted=encrypted, sha256=sha,
                                  size_bytes=size, note="cli backup create")
    except Exception:
        pass
    c.success(f"Backup complete → {path}  ({_human_bytes(size)})")
    c.dim("  View past backups: navig mobile backup list")


@mobile_app.command("restore", rich_help_panel="Backup & Restore")
def cmd_restore(
    src: Annotated[str, typer.Argument(help="Backup dir (iOS) or .ab file/dir (Android)")],
    udid: UdidOpt = None,
    password: Annotated[Optional[str], typer.Option("--password", "-p", help="iOS backup password.")] = None,
    yes: YesOpt = False,
) -> None:
    """Restore a backup — DESTRUCTIVE (overwrites device data). Consent + --yes required."""
    from navig_mobile.engine.base import DeviceError

    if not Path(src).exists():
        c.error(f"Backup path not found: {src}")
        raise typer.Exit(2)
    dev = _resolve(udid)
    _require_consent(dev.udid, "restore")
    if not _confirm_destructive(f"restore backup {src} → {dev.udid}", dev.udid, yes):
        raise typer.Exit(0)
    try:
        _run_with_spinner(
            lambda: dev.restore(src, password=password,
                                progress=lambda d, t, n: n and c.dim("  " + n)),
            "restoring…")
    except DeviceError as exc:
        c.error(str(exc))
        raise typer.Exit(1) from exc
    c.success(f"Restore requested for {dev.udid}. The device may reboot.")


# ═════════════════════════ Screen & Control ═════════════════════════════════

@mobile_app.command("screenshot", rich_help_panel="Screen & Control")
def cmd_screenshot(
    udid: UdidOpt = None,
    out: Annotated[Optional[Path], typer.Option("--out", "-o", help="Output PNG path.")] = None,
) -> None:
    """Capture the device screen to a PNG."""
    from navig_mobile.engine.base import DeviceError

    dev = _resolve(udid)
    dest = out or (Path.cwd() / "screenshot.png")
    try:
        saved = dev.screenshot(str(dest))
    except DeviceError as exc:
        c.error(str(exc))
        raise typer.Exit(1) from exc
    c.success(f"Screenshot → {saved}")


@mobile_app.command("mirror", rich_help_panel="Screen & Control")
def cmd_mirror(udid: UdidOpt = None) -> None:
    """Mirror/control the screen (scrcpy on Android)."""
    import shutil
    import subprocess

    scrcpy = shutil.which("scrcpy")
    if not scrcpy:
        c.error("scrcpy not found — see `navig mobile doctor` for install.")
        raise typer.Exit(127)
    argv = [scrcpy] + (["-s", udid] if udid else [])
    c.step("Launching scrcpy … (close the window to stop)")
    subprocess.run(argv)


# ═════════════════════════ Staged pillars (visible now, land later) ═════════

@mobile_app.command("forensics", rich_help_panel="Forensics & Investigate")
def cmd_forensics(
    action: Annotated[str, typer.Argument(help="acquire | parse | report | verify | hash")] = "acquire",
    target: Annotated[Optional[str], typer.Argument(help="path (for parse/hash)")] = None,
    udid: UdidOpt = None,
    case: Annotated[Optional[str], typer.Option("--case", help="Case id (append to / read from).")] = None,
    platform: Annotated[Optional[str], typer.Option("--platform", help="android|ios (for parse).")] = None,
    input_type: Annotated[Optional[str], typer.Option("--type", help="LEAPP input type override.")] = None,
    out: Annotated[Optional[Path], typer.Option("--out", "-o", help="Output dir (parse).")] = None,
    json_out: JsonOpt = False,
) -> None:
    """Consent-gated logical acquisition + artifact parsing (iLEAPP/ALEAPP) → case dir."""
    from navig_mobile.consent import CaseDir
    from navig_mobile.engine.base import DeviceError
    from navig_mobile.engine.forensics import acquire as acq_mod

    if action == "acquire":
        dev = _resolve(udid)
        rec = _require_consent(dev.udid, "forensics acquire")
        info = dev.info()
        cd = _open_or_new_case(case, udid=dev.udid, platform=info.platform.value,
                               name=f"acquire {info.name or dev.udid}",
                               authorization_ref=rec.get("authorization_ref", ""),
                               scope=rec.get("scope", ""))
        c.info(f"Acquiring {info.name or dev.udid} → case {cd.case_id}")
        c.dim("  Consent on file; evidence hashed into the case manifest.")
        try:
            summary = _run_with_spinner(
                lambda: acq_mod.acquire(dev, cd, progress=lambda d, t, n: n and c.dim("  " + n)),
                "acquiring…")
        except DeviceError as exc:
            c.error(str(exc))
            raise typer.Exit(1) from exc
        _record_case(cd)
        if json_out:
            _emit_json({"case": cd.case_id, **summary})
        else:
            c.success(f"Acquired {summary['type']} → {summary['path']}")
            c.dim(f"  case: {cd.case_id}  ·  next: navig mobile scan spyware --case {cd.case_id}")
        return

    if action == "parse":
        _need(target or case, "parse needs an extraction path or --case")
        cd = CaseDir.open(_case_root(case)) if case else None
        src = target or (str(cd.subdir("acquisition")) if cd else "")
        plat = platform or (cd.platform if cd else None)
        _need(plat, "parse needs --platform android|ios (or a --case that records it)")
        out_dir = str(out or (cd.subdir("parse") if cd else Path.cwd() / "leapp-report"))
        _run_leapp_parse(plat, src, out_dir, input_type, cd, json_out)
        return

    if action in ("report", "verify"):
        _need(case, f"{action} needs --case")
        cd = CaseDir.open(_case_root(case))
        if action == "verify":
            _print_verify(cd, json_out)
        else:
            _print_case(cd, json_out)
        return

    if action == "hash":
        _need(target, "hash needs a path")
        from navig_mobile.consent import _hash_path

        sha, size = _hash_path(Path(target))
        (_emit_json({"path": target, "sha256": sha, "bytes": size}) if json_out
         else c.info(f"sha256={sha}  bytes={size}"))
        return

    c.error(f"Unknown forensics action {action!r}. Use acquire/parse/report/verify/hash.")
    raise typer.Exit(2)


@mobile_app.command("scan", rich_help_panel="Spyware Scan")
def cmd_scan(
    action: Annotated[str, typer.Argument(help="spyware | backup | iocs")] = "spyware",
    target: Annotated[Optional[str], typer.Argument(help="acquisition path (backup) / results dir (iocs)")] = None,
    udid: UdidOpt = None,
    case: Annotated[Optional[str], typer.Option("--case", help="Case id to acquire into / read from.")] = None,
    platform: Annotated[Optional[str], typer.Option("--platform", help="android|ios (for backup/iocs).")] = None,
    iocs: Annotated[Optional[str], typer.Option("--iocs", help="STIX2 IOC feed file.")] = None,
    out: Annotated[Optional[Path], typer.Option("--out", "-o", help="MVT results dir.")] = None,
    json_out: JsonOpt = False,
) -> None:
    """Scan a device/backup for spyware with MVT (Pegasus/mercenary IOCs). Consent-gated."""
    from navig_mobile.engine.base import DeviceError
    from navig_mobile.engine.forensics import acquire as acq_mod
    from navig_mobile.engine.forensics import spyware

    try:
        if action == "spyware":
            dev = _resolve(udid)
            rec = _require_consent(dev.udid, "spyware scan")
            info = dev.info()
            if not spyware.find_mvt(info.platform.value):
                c.error(str(spyware.MvtUnavailable(info.platform.value)))
                raise typer.Exit(3)
            cd = _open_or_new_case(case, udid=dev.udid, platform=info.platform.value,
                                   name=f"scan {info.name or dev.udid}",
                                   authorization_ref=rec.get("authorization_ref", ""),
                                   scope=rec.get("scope", ""))
            c.info(f"Acquiring + scanning {info.name or dev.udid} → case {cd.case_id}")
            summary = _run_with_spinner(
                lambda: acq_mod.acquire(dev, cd, progress=lambda d, t, n: n and c.dim("  " + n)),
                "acquiring…")
            _record_case(cd)
            res = _run_with_spinner(
                lambda: spyware.scan(info.platform.value, summary["path"],
                                     str(cd.subdir("scan")), iocs=iocs),
                "running MVT…")
            cd.add_evidence(Path(res["output"]), tool=res["tool"], source=f"case:{cd.case_id}",
                            note="mvt results")
            cd.log_event("scanned", res["tool"])
            _report_scan(res, cd.case_id, json_out)
            return

        if action == "backup":
            _need(target, "backup scan needs an acquisition path")
            _need(platform, "backup scan needs --platform android|ios")
            if not spyware.find_mvt(platform):
                c.error(str(spyware.MvtUnavailable(platform)))
                raise typer.Exit(3)
            out_dir = str(out or (Path.cwd() / "mvt-results"))
            res = _run_with_spinner(
                lambda: spyware.scan(platform, target, out_dir, iocs=iocs), "running MVT…")
            _report_scan(res, None, json_out)
            return

        if action == "iocs":
            _need(target, "iocs needs an MVT results dir")
            _need(platform, "iocs needs --platform android|ios")
            _need(iocs, "iocs needs --iocs <stix2 feed>")
            res = spyware.check_iocs(platform, target, iocs)
            _emit_json(res) if json_out else c.info(
                f"IOC re-check: {res['detections']} detection(s) (exit {res['returncode']}).")
            return

        c.error(f"Unknown scan action {action!r}. Use spyware/backup/iocs.")
        raise typer.Exit(2)
    except spyware.MvtUnavailable as exc:
        c.error(str(exc))
        raise typer.Exit(3) from exc  # capability-missing → 3 (consistent across scan)
    except DeviceError as exc:
        c.error(str(exc))
        raise typer.Exit(1) from exc


@mobile_app.command("root", rich_help_panel="Rooting & Jailbreak")
def cmd_root(
    action: Annotated[str, typer.Argument(help="status | guide | unlock | flash")] = "status",
    udid: UdidOpt = None,
    partition: Annotated[Optional[str], typer.Option("--partition", help="Target partition (flash).")] = None,
    image: Annotated[Optional[str], typer.Option("--image", help="Image file (flash).")] = None,
    yes: YesOpt = False,
    json_out: JsonOpt = False,
) -> None:
    """Rooting assist (Android): status · guide · unlock · flash. Never one-click —
    unlock/flash need recorded consent + explicit --yes and wipe the device."""
    from navig_mobile.engine.base import DeviceError, Platform
    from navig_mobile.engine.rooting import android as rooting

    if action == "status":
        dev = _resolve(udid, platform="android")
        if getattr(dev, "platform", None) is not Platform.ANDROID:
            c.error("root status is Android-only — use `navig mobile jailbreak status` for iOS.")
            raise typer.Exit(2)
        try:
            st = rooting.root_status(dev)
        except DeviceError as exc:
            c.error(str(exc))
            raise typer.Exit(1) from exc
        _emit_json(st) if json_out else _print_root_status(st)
        return

    if action == "guide":
        _print_root_guide()
        return

    if action in ("unlock", "flash"):
        if not rooting.find_fastboot():
            c.error("`fastboot` not found — install Android platform-tools (see `navig mobile doctor`).")
            raise typer.Exit(127)
        if action == "flash":
            _need(partition, "flash needs --partition (e.g. boot)")
            _need(image, "flash needs --image <file>")
            desc = f"fastboot flash {partition} {image}"
        else:
            desc = "fastboot flashing unlock — UNLOCK THE BOOTLOADER"
        fbs = rooting.fastboot_devices()
        target = udid or (fbs[0] if len(fbs) == 1 else None)
        if not target:
            if not fbs:
                c.error("No device in fastboot mode. Reboot to the bootloader first: "
                        "`navig mobile android reboot bootloader`, then reconnect.")
                raise typer.Exit(1)
            c.error("Multiple fastboot devices — pass --udid.")
            raise typer.Exit(2)
        _require_consent(target, f"root {action}")
        if not _confirm_destructive(desc, target, yes):
            raise typer.Exit(0)
        try:
            rc, out = (rooting.unlock(target) if action == "unlock"
                       else rooting.flash(partition, image, target))
        except DeviceError as exc:
            c.error(str(exc))
            raise typer.Exit(3) from exc
        (c.success if rc == 0 else c.error)(out or f"fastboot exit {rc}")
        raise typer.Exit(0 if rc == 0 else 1)

    c.error(f"Unknown root action {action!r}. Use status/guide/unlock/flash.")
    raise typer.Exit(2)


@mobile_app.command("jailbreak", rich_help_panel="Rooting & Jailbreak")
def cmd_jailbreak(
    action: Annotated[str, typer.Argument(help="status | guide")] = "status",
    udid: UdidOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Jailbreak assist (iOS): status (checkm8 eligibility + dev-mode) · guide (palera1n/checkra1n)."""
    from navig_mobile.engine.base import DeviceError, Platform
    from navig_mobile.engine.rooting import ios as jb

    if action == "status":
        dev = _resolve(udid, platform="ios")
        if getattr(dev, "platform", None) is not Platform.IOS:
            c.error("jailbreak status is iOS-only — use `navig mobile root status` for Android.")
            raise typer.Exit(2)
        try:
            st = jb.jailbreak_status(dev)
        except DeviceError as exc:
            c.error(str(exc))
            raise typer.Exit(1) from exc
        _emit_json(st) if json_out else _print_jailbreak_status(st)
        return

    if action == "guide":
        _print_jailbreak_guide()
        return

    c.error(f"Unknown jailbreak action {action!r}. Use status/guide.")
    raise typer.Exit(2)


@mobile_app.command("dev", rich_help_panel="Dev-Tools")
def cmd_dev(
    action: Annotated[str, typer.Argument(help="shell | enable | frida | attach | spawn | objection")] = "shell",
    target: Annotated[Optional[str], typer.Argument(help="process/app id (attach/spawn/objection)")] = None,
    udid: UdidOpt = None,
) -> None:
    """Developer tools — shell, enable dev-mode, frida (list/attach/spawn), objection."""
    if action == "shell":
        import shutil
        import subprocess

        adb = shutil.which("adb")
        if adb:
            subprocess.run([adb] + (["-s", udid] if udid else []) + ["shell"])
            return
        c.error("Interactive shell currently supports Android (adb). See `navig mobile doctor`.")
        raise typer.Exit(127)

    if action == "enable":
        from navig_mobile.engine.base import DeviceError, Platform

        dev = _resolve(udid)
        if dev.platform is Platform.IOS:
            from navig_mobile.engine.rooting import ios as jb

            try:
                out = jb.enable_developer_mode(dev.udid)
            except DeviceError as exc:
                c.error(str(exc))
                raise typer.Exit(1) from exc
            c.success("Requested iOS Developer Mode — the device reboots; confirm the "
                      "on-device prompt (Settings › Privacy & Security › Developer Mode).")
            if out.strip():
                c.dim("  " + out.strip()[:200])
        else:
            c.info("Android: enable Developer Options (Settings › About › tap 'Build number' "
                   "7×), then turn on 'USB debugging' under Developer options.")
        return

    if action in ("frida", "attach", "spawn", "objection"):
        from navig_mobile.engine.base import DeviceError
        from navig_mobile.engine.devtools import frida as fr

        try:
            if action == "frida":
                rc, out = fr.ps(udid, apps=True)
                typer.echo(out.strip())
                raise typer.Exit(rc)
            _need(target, f"{action} needs a target (app id / process name)")
            if action == "objection":
                c.step(f"Launching objection against {target} … (Ctrl-D / exit to quit)")
                raise typer.Exit(fr.objection(target, udid))
            c.step(f"frida {action} {target} … (Ctrl-D / exit to quit)")
            raise typer.Exit(fr.attach(target, udid) if action == "attach"
                             else fr.spawn(target, udid))
        except fr.FridaUnavailable as exc:
            c.error(str(exc))
            raise typer.Exit(127) from exc
        except DeviceError as exc:
            c.error(str(exc))
            raise typer.Exit(127) from exc

    _staged(f"dev {action}", "a later stage")


@mobile_app.command("osint", rich_help_panel="OSINT & Case")
def cmd_osint(
    action: Annotated[str, typer.Argument(help="device | apps | report")] = "device",
    target: Annotated[Optional[str], typer.Argument(help="package id (apps <pkg> for one app)")] = None,
    udid: UdidOpt = None,
    limit: Annotated[int, typer.Option("--limit", help="Max apps to scan (apps/report).")] = 40,
    out: Annotated[Optional[Path], typer.Option("--out", "-o", help="Report output path.")] = None,
    json_out: JsonOpt = False,
) -> None:
    """Device-anchored OSINT: identity · app-privacy report · combined report."""
    from navig_mobile.engine.base import DeviceError
    from navig_mobile.engine.osint import report as osint

    dev = _resolve(udid)
    try:
        if action == "device":
            ident = osint.device_identity(dev)
            _emit_json(ident) if json_out else _print_identity(ident)
            return
        if action == "apps":
            if target:
                prof = osint.app_permissions(dev, target)
                _emit_json(prof) if json_out else _print_app_privacy(prof)
            else:
                scan = osint.app_privacy_scan(dev, limit=limit)
                _emit_json(scan) if json_out else _print_privacy_scan(scan)
            return
        if action == "report":
            rep = osint.build_report(dev, limit=limit)
            path = osint.write_report(rep, str(out) if out else None)
            if json_out:
                _emit_json({**rep, "saved": path})
            else:
                _print_identity(rep["identity"])
                _print_privacy_scan(rep["app_privacy"])
                c.success(f"OSINT report saved → {path}")
                c.dim("  " + rep["handoff"])
            return
        c.error(f"Unknown osint action {action!r}. Use device/apps/report.")
        raise typer.Exit(2)
    except DeviceError as exc:
        c.error(str(exc))
        raise typer.Exit(1) from exc


@mobile_app.command("consent", rich_help_panel="OSINT & Case")
def cmd_consent(
    action: Annotated[str, typer.Argument(help="record | show | revoke")] = "show",
    udid: UdidOpt = None,
    authorization: Annotated[str, typer.Option(
        "--authorization", "-a",
        help='Authorization reference (e.g. "I own this device", a ticket, or a signed-agreement ref).')] = "",
    scope: Annotated[str, typer.Option("--scope", help="What's in scope for this device.")] = "",
    until: Annotated[Optional[str], typer.Option("--until", help="Expiry (YYYY-MM-DD).")] = None,
    note: Annotated[str, typer.Option("--note", help="Free-text note.")] = "",
    json_out: JsonOpt = False,
) -> None:
    """Record/show/revoke authorization to investigate a device (required before
    forensics/spyware/media pull)."""
    from navig_mobile.consent import ConsentGate, ConsentRefused

    gate = ConsentGate()
    target = udid or _resolve_udid_only()
    if not target:
        raise typer.Exit(1)

    if action == "record":
        try:
            gate.record(udid=target, authorization_ref=authorization, scope=scope,
                        until=until, note=note)
        except ConsentRefused as exc:
            c.error(str(exc))
            raise typer.Exit(2) from exc
        c.success(f"Authorization recorded for {target}.")
        c.dim("  You can now run forensics/spyware/media on this device. "
              "Revoke with `navig mobile consent revoke -u <udid>`.")
        return

    if action == "revoke":
        n = gate.revoke(target)
        c.success(f"Revoked {n} consent record(s) for {target}." if n
                  else f"No active consent to revoke for {target}.")
        return

    # show
    rec = gate.check(target)
    if json_out:
        _emit_json(rec or {})
        return
    if not rec:
        c.warning(f"No active authorization for {target}.")
        c.dim(f'  Record it: navig mobile consent record -u {target} '
              f'--authorization "I own this device" --scope "..."')
        return
    c.print_rows(["Field", "Value"], [
        ["Device", target],
        ["Authorization", rec.get("authorization_ref", "")],
        ["Scope", rec.get("scope", "") or "—"],
        ["Operator", rec.get("operator", "") or "—"],
        ["Granted", (rec.get("granted_at", "") or "")[:19]],
        ["Expires", (rec.get("expires_at") or "no expiry")[:19]],
    ], title=f"Authorization — {target}")


@mobile_app.command("case", rich_help_panel="OSINT & Case")
def cmd_case(
    action: Annotated[str, typer.Argument(help="list | show")] = "list",
    case_id: Annotated[Optional[str], typer.Argument(help="case id (for show)")] = None,
    json_out: JsonOpt = False,
) -> None:
    """List investigation cases, or show one's evidence manifest."""
    from navig_mobile.consent import CaseDir
    from navig_mobile.store import get_store

    if action == "list":
        cases = get_store().list_cases()
        if json_out:
            _emit_json(cases)
            return
        if not cases:
            c.warning("No cases yet. Run `navig mobile forensics acquire` or `navig mobile scan spyware`.")
            return
        rows = [[k["case_id"], k.get("platform") or "—", k.get("udid") or "—",
                 k.get("status") or "—", (k.get("created_at", "") or "")[:19],
                 k.get("name") or ""] for k in cases]
        c.print_rows(["Case", "Platform", "UDID", "Status", "Created", "Name"], rows,
                     title=f"{len(cases)} case(s)")
        return

    if action == "show":
        _need(case_id, "show needs a case id")
        cd = CaseDir.open(_case_root(case_id))
        _print_case(cd, json_out)
        return

    c.error(f"Unknown case action {action!r}. Use list/show.")
    raise typer.Exit(2)


@mobile_app.command("evidence", rich_help_panel="OSINT & Case")
def cmd_evidence(
    case_id: Annotated[str, typer.Argument(help="case id")],
    verify: Annotated[bool, typer.Option("--verify", help="Re-hash artifacts and check integrity.")] = False,
    json_out: JsonOpt = False,
) -> None:
    """List a case's evidence (or --verify their integrity)."""
    from navig_mobile.consent import CaseDir

    cd = CaseDir.open(_case_root(case_id))
    if verify:
        _print_verify(cd, json_out)
    else:
        _print_case(cd, json_out)


# ═════════════════════════ Android-only ═════════════════════════════════════

@android_app.command("logcat", rich_help_panel="Android")
def cmd_logcat(
    udid: UdidOpt = None,
    lines: Annotated[int, typer.Option("--lines", "-n", help="Recent lines to show.")] = 200,
) -> None:
    """Show recent Android logcat lines."""
    from navig_mobile.engine.base import DeviceError, Platform

    dev = _resolve(udid, platform="android")
    if getattr(dev, "platform", None) is not Platform.ANDROID:
        c.error("logcat is Android-only.")
        raise typer.Exit(2)
    try:
        out = dev._raw.shell(f"logcat -d -t {int(lines)}")  # type: ignore[attr-defined]
    except DeviceError as exc:
        c.error(str(exc))
        raise typer.Exit(1) from exc
    except Exception as exc:  # backend/shell error
        c.error(f"logcat failed: {exc}")
        raise typer.Exit(1) from exc
    typer.echo(out)


@android_app.command("connect", rich_help_panel="Android")
def cmd_android_connect(
    address: Annotated[str, typer.Argument(help="host:port — e.g. 192.168.1.20:5555")],
) -> None:
    """adb connect over TCP/IP."""
    cmd_connect(address)


@android_app.command("reboot", rich_help_panel="Android")
def cmd_android_reboot(
    mode: Annotated[str, typer.Argument(help="system | bootloader | recovery")] = "system",
    udid: UdidOpt = None,
) -> None:
    """Reboot the device (into the bootloader/recovery for rooting flows)."""
    from navig_mobile.engine.base import DeviceError

    dev = _resolve(udid, platform="android")
    if mode not in ("system", "bootloader", "recovery", "fastboot"):
        c.error("mode must be system | bootloader | recovery.")
        raise typer.Exit(2)
    try:
        c.step(f"Rebooting {dev.udid} → {mode} …")
        dev.reboot(mode)
    except DeviceError as exc:
        c.error(str(exc))
        raise typer.Exit(1) from exc
    c.success(f"Reboot to {mode} requested.")
    if mode in ("bootloader", "fastboot"):
        c.dim("  Once in fastboot: navig mobile root unlock  (or  navig mobile android fastboot devices)")


@android_app.command("fastboot", rich_help_panel="Android")
def cmd_fastboot(
    args: Annotated[Optional[list[str]], typer.Argument(help="fastboot arguments (e.g. devices)")] = None,
) -> None:
    """Pass arguments straight through to the fastboot binary (device in bootloader)."""
    from navig_mobile.engine.rooting import android as rooting

    if not rooting.find_fastboot():
        c.error("`fastboot` not found — install Android platform-tools (see `navig mobile doctor`).")
        raise typer.Exit(127)
    if not args:
        c.info("Usage: navig mobile android fastboot <args>  (e.g. `fastboot devices`)")
        raise typer.Exit(0)
    rc, out, err = rooting._run_fastboot(list(args), timeout=600)
    typer.echo((out + err).strip())
    raise typer.Exit(0 if rc == 0 else 1)


@android_app.command("sideload", rich_help_panel="Android")
def cmd_sideload(
    zip_path: Annotated[str, typer.Argument(help="OTA/Magisk .zip to sideload (device in recovery)")],
    udid: UdidOpt = None,
    yes: YesOpt = False,
) -> None:
    """adb sideload a zip (device must be in recovery). Consent + --yes required."""
    from navig_mobile.engine.base import DeviceError
    from navig_mobile.engine.rooting import android as rooting

    target = udid or _resolve_udid_only()
    _require_consent(target, "android sideload")
    if not _confirm_destructive(f"adb sideload {zip_path}", target, yes):
        raise typer.Exit(0)
    try:
        rc, out = rooting.sideload(zip_path, target)
    except DeviceError as exc:
        c.error(str(exc))
        raise typer.Exit(3) from exc
    typer.echo(out)
    raise typer.Exit(0 if rc == 0 else 1)


# ═════════════════════════ iOS-only ═════════════════════════════════════════

@ios_app.command("crashlogs", rich_help_panel="iOS")
def cmd_crashlogs(
    udid: UdidOpt = None,
    out: Annotated[Optional[Path], typer.Option("--out", "-o", help="Destination dir.")] = None,
    list_only: Annotated[bool, typer.Option("--list", help="List crash reports without pulling.")] = False,
) -> None:
    """Pull (or list) iOS crash reports."""
    from navig_mobile.engine.base import DeviceError
    from navig_mobile.engine.devtools import ios_dev

    dev = _resolve(udid, platform="ios")
    try:
        if list_only:
            typer.echo(ios_dev.crash_ls(dev.udid))
            return
        dest = str(out or (Path.cwd() / f"crashlogs-{dev.udid}"))
        c.step("Pulling iOS crash reports …")
        saved = ios_dev.crash_pull(dev.udid, dest)
    except DeviceError as exc:
        c.error(str(exc))
        raise typer.Exit(1) from exc
    c.success(f"Crash reports → {saved}")


@ios_app.command("syslog", rich_help_panel="iOS")
def cmd_syslog(udid: UdidOpt = None) -> None:
    """Stream live iOS syslog (Ctrl-C to stop)."""
    from navig_mobile.engine.devtools import ios_dev

    dev = _resolve(udid, platform="ios")
    c.step("Streaming iOS syslog — Ctrl-C to stop …")
    raise typer.Exit(ios_dev.syslog_live(dev.udid))


@ios_app.command("pcap", rich_help_panel="iOS")
def cmd_ios_pcap(
    udid: UdidOpt = None,
    out: Annotated[Optional[Path], typer.Option("--out", "-o", help="Output .pcap path.")] = None,
) -> None:
    """Capture device network traffic to a .pcap (Ctrl-C to stop)."""
    from navig_mobile.engine.devtools import ios_dev

    dev = _resolve(udid, platform="ios")
    dest = str(out or (Path.cwd() / "capture.pcap"))
    c.step(f"Capturing packets → {dest}  (Ctrl-C to stop) …")
    c.dim("  iOS 17+ needs an elevated `pymobiledevice3 remote tunneld` running.")
    raise typer.Exit(ios_dev.pcap(dev.udid, dest))


@ios_app.command("instruments", rich_help_panel="iOS")
def cmd_ios_instruments(
    args: Annotated[Optional[list[str]], typer.Argument(
        help="DVT subcommand + args (e.g. proclist, applist, kill <pid>)")] = None,
    udid: UdidOpt = None,
) -> None:
    """Drive DVT instruments (proclist/applist/kill/signal/…) — passthrough to
    `pymobiledevice3 developer dvt`."""
    from navig_mobile.engine.devtools import ios_dev

    dev = _resolve(udid, platform="ios")
    if not args:
        c.info("Usage: navig mobile ios instruments <proclist|applist|kill|signal|…> [args]")
        c.dim("  iOS 17+ needs an elevated `pymobiledevice3 remote tunneld` running.")
        raise typer.Exit(0)
    raise typer.Exit(ios_dev.dvt(dev.udid, list(args)))


@ios_app.command("recovery", rich_help_panel="iOS")
def cmd_ios_recovery(
    action: Annotated[str, typer.Argument(help="enter | exit")] = "enter",
    udid: UdidOpt = None,
    yes: YesOpt = False,
) -> None:
    """Enter/exit iOS Recovery mode (drives pymobiledevice3 restore enter/exit)."""
    from navig_mobile.engine.base import DeviceError, Platform
    from navig_mobile.engine.rooting import ios as jb

    dev = _resolve(udid, platform="ios")
    if getattr(dev, "platform", None) is not Platform.IOS:
        c.error("recovery is iOS-only.")
        raise typer.Exit(2)
    if action not in ("enter", "exit"):
        c.error("action must be enter | exit.")
        raise typer.Exit(2)
    if action == "enter" and not _confirm_destructive(
            "enter Recovery mode (device becomes temporarily unusable until exit)",
            dev.udid, yes):
        raise typer.Exit(0)
    try:
        out = jb.enter_recovery(dev.udid) if action == "enter" else jb.exit_recovery(dev.udid)
    except DeviceError as exc:
        c.error(str(exc))
        raise typer.Exit(1) from exc
    c.success(f"Recovery {action} requested.")
    if out.strip():
        c.dim("  " + out.strip()[:200])


@ios_app.command("dfu", rich_help_panel="iOS")
def cmd_dfu(udid: UdidOpt = None) -> None:
    """Guide entering DFU mode (button timing is physical — can't be automated)."""
    c.info("DFU mode requires a precise physical button sequence — it can't be driven over USB.")
    c.plain()
    c.step("Face ID iPhones / recent iPads:")
    c.dim("  1) Plug into the computer.  2) Quick-press Volume Up, then Volume Down.")
    c.dim("  3) Hold the Side button until the screen goes black, then ALSO hold Volume Down.")
    c.dim("  4) After ~5s release the Side button but KEEP holding Volume Down ~10s.")
    c.dim("  Screen stays black = DFU. (Anything on screen = not DFU; retry.)")
    c.plain()
    c.info("For the softer Recovery mode (drivable): navig mobile ios recovery enter")


# ═════════════════════════ App UI automation (agent-device) ═════════════════
# Drive/verify app UIs — the capability the device engines don't cover. Every
# verb wraps the detected `agent-device` CLI (MIT, Node) as a subprocess; missing
# binary → exit 127 with install guidance (mirrors scrcpy/frida). agent-device is
# session-oriented: `open` binds a platform/device, then snapshot/tap/… act on it.

def _ui():
    from navig_mobile.engine.uiauto import agent_device

    return agent_device


def _ui_guard():
    """Return the agent-device module, or exit 127 with install guidance."""
    ui = _ui()
    if not ui.available():
        c.error("agent-device not found — install it to drive/verify app UIs.")
        c.dim("  npm install -g agent-device@latest   (needs Node 22+)")
        c.dim("  Then: agent-device doctor  ·  navig mobile doctor")
        raise typer.Exit(127)
    return ui


def _ui_emit(rc: int, out: str, hint: str = "") -> None:
    """Echo an agent-device subcommand's output and exit with its return code."""
    text = (out or "").strip()
    if text:
        typer.echo(text)
    if rc == 0 and hint:
        c.dim("  " + hint)
    raise typer.Exit(rc)


def _check_platform(platform: Optional[str]) -> None:
    from navig_mobile.engine.uiauto.agent_device import PLATFORMS

    if platform is not None and platform not in PLATFORMS:
        c.error(f"Unknown --platform {platform!r}. Use one of: {', '.join(PLATFORMS)}.")
        raise typer.Exit(2)


def _uiauto_notice() -> None:
    c.dim("  UI automation drives a real app — only drive apps/devices you own or "
          "are authorized to test.")


# Markers that identify a simulator/emulator (benign dev targets) rather than a
# physical device you must be authorized to drive.
_UI_SIM_MARKERS = ("emulator-", "emulator", "simulator", "localhost:", "127.0.0.1:")
# A canonical UUID (8-4-4-4-12) is an Xcode/iOS *simulator* udid; a physical iOS
# udid is 40 hex chars (older) or `00008xxx-…` 25-char/one-dash (A12+), and an
# Android serial is short alphanumeric — none of which match this shape.
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _looks_physical(udid: Optional[str]) -> bool:
    """Heuristic: an explicitly-named target that carries no sim/emulator marker is
    treated as a physical device (errs toward safety — gating is a one-time consent
    record). No target (agent-device auto-picks the booted simulator/emulator) →
    not physical; a canonical-UUID target is an iOS simulator → not physical."""
    if not udid:
        return False
    if _UUID_RE.match(udid.strip()):
        return False
    u = udid.lower()
    return not any(m in u for m in _UI_SIM_MARKERS)


def _ui_require_ownership(udid: Optional[str], action: str) -> None:
    """Gate a session-starting verb. Driving a *physical* device needs a recorded
    authorization (reuses the forensics consent gate → exit 4 if absent). Simulator/
    emulator/auto targets are benign dev work → a soft ownership reminder."""
    if _looks_physical(udid):
        _require_consent(udid, f"ui {action}")
    else:
        _uiauto_notice()


def _ui_case(case_id: str, *, platform: Optional[str], device: Optional[str]):
    """Open (or friendly-create) an evidence case for UI captures. Unlike the
    forensics gate, UI evidence is dev-grade provenance — no consent required, and
    a named case is created on first use rather than erroring."""
    from navig_mobile import config
    from navig_mobile.consent import CaseDir

    root = config.mobile_dir() / "cases" / case_id
    if root.exists():
        return CaseDir.open(root)
    cd = CaseDir.create(case_id=case_id, udid=device or "uiauto",
                        platform=platform or "uiauto", name="ui verification",
                        authorization_ref="app UI verification (dev)")
    _record_case(cd)
    return cd


# ── assert: matching + settling ──────────────────────────────────────────────
# An agent-device element ref (@e3). Refs must match EXACTLY: a plain substring test
# lets "@e20" satisfy a check for "@e2", so `assert @e2` would pass on a screen where
# @e2 doesn't exist — a false-positive in the one primitive whose job is proving a
# claim. Free text stays a substring match (that's what a human means by "shows X").
_UI_REF_RE = re.compile(r"^@[A-Za-z]*\d+$")


def _ui_occurrences(screen: str, needle: str) -> int:
    """How many times `needle` appears in a snapshot. Element refs match exactly
    (never as a prefix of a longer ref); free text is a substring count."""
    if not screen or not needle:
        return 0
    if _UI_REF_RE.match(needle):
        return len(re.findall(rf"(?<![\w@]){re.escape(needle)}(?!\d)", screen))
    return screen.count(needle)


class _UiCheck(NamedTuple):
    ok: bool         # the assertion holds
    readable: bool   # the screen could be snapshotted at all
    count: int       # occurrences of the needle


def _ui_settle(check, timeout: float, interval: float = 0.4) -> tuple[_UiCheck, float]:
    """Re-run `check` until the assertion holds or `timeout` elapses; return the last
    result and how long we waited.

    A UI is asynchronous: a screen that renders 300ms after the tap is *correct*, not
    a failure. A single-shot assert turns that latency into a flaky red — so the verify
    primitive waits for the screen to settle instead. Only the failing path pays the
    wait (a screen that is already right returns on the first check). A screen that
    can't be read yet is retried too — mid-transition it is transient, not a verdict.
    `timeout <= 0` → check once (no wait).
    """
    start = time.monotonic()
    deadline = start + max(0.0, timeout)
    while True:
        res = check()
        if res.ok or time.monotonic() >= deadline:
            return res, time.monotonic() - start
        time.sleep(interval)


@ui_app.command("doctor", rich_help_panel="Setup")
def ui_doctor() -> None:
    """agent-device's own environment check (SDKs, drivers, permissions)."""
    ui = _ui_guard()
    v = ui.version()
    if v:
        c.info(f"agent-device {v}")
    rc, out = ui.doctor()
    _ui_emit(rc, out, hint="Drive an app: navig mobile ui open <app> -p <platform>")


@ui_app.command("devices", rich_help_panel="Discovery")
def ui_devices(json_out: JsonOpt = False) -> None:
    """List app-automation targets (simulators / emulators / devices)."""
    ui = _ui_guard()
    rc, out = ui.list_devices(json_out=json_out)
    _ui_emit(rc, out)


@ui_app.command("apps", rich_help_panel="Discovery")
def ui_apps(platform: PlatformOpt = None, udid: UdidOpt = None, json_out: JsonOpt = False) -> None:
    """List launchable apps for a platform."""
    _check_platform(platform)
    ui = _ui_guard()
    rc, out = ui.list_apps(platform, udid, json_out=json_out)
    _ui_emit(rc, out)


@ui_app.command("open", rich_help_panel="Session")
def ui_open(
    app: Annotated[str, typer.Argument(help="App name / bundle id / package to open")],
    platform: PlatformOpt = None,
    udid: UdidOpt = None,
) -> None:
    """Start a UI session bound to an app — snapshot/tap/… then act on it."""
    _check_platform(platform)
    ui = _ui_guard()
    _ui_require_ownership(udid, "open")
    rc, out = ui.open_app(app, platform, udid)
    _ui_emit(rc, out, hint="Session open. Next: navig mobile ui snapshot -i · ui tap @e2 · ui close")


@ui_app.command("close", rich_help_panel="Session")
def ui_close() -> None:
    """End the current UI session."""
    ui = _ui_guard()
    rc, out = ui.close()
    _ui_emit(rc, out)


@ui_app.command("snapshot", rich_help_panel="Inspect")
def ui_snapshot(
    interactive: Annotated[bool, typer.Option(
        "--interactive/--full", "-i",
        help="Interactive (actionable) elements only — the default; --full = whole tree.")] = True,
    json_out: JsonOpt = False,
    case: CaseOpt = None,
    platform: PlatformOpt = None,
    udid: UdidOpt = None,
) -> None:
    """Accessibility snapshot with element refs (@e1…) — the agent's eyes."""
    ui = _ui_guard()
    rc, out = ui.snapshot(interactive=interactive, json_out=json_out)
    text = (out or "").strip()
    if text:
        typer.echo(text)
    if case is not None and text:
        cd = _ui_case(case, platform=platform, device=udid)
        ext = "json" if json_out else "txt"
        n = len(cd.manifest.get("artifacts", [])) + 1
        p = cd.subdir("uiauto") / f"snapshot-{n:03d}.{ext}"
        p.write_text(text, encoding="utf-8")
        cd.add_evidence(p, tool="agent-device", source="ui snapshot", note="a11y snapshot")
        c.dim(f"  evidence → case {cd.case_id}")
    raise typer.Exit(rc)


@ui_app.command("screenshot", rich_help_panel="Inspect")
def ui_screenshot(
    out: Annotated[Optional[Path], typer.Option("--out", "-o", help="Output PNG path.")] = None,
    case: CaseOpt = None,
    platform: PlatformOpt = None,
    udid: UdidOpt = None,
) -> None:
    """Capture the app screen to a PNG (app-context — distinct from the raw
    device-frame `navig mobile screenshot`)."""
    ui = _ui_guard()
    cd = _ui_case(case, platform=platform, device=udid) if case is not None else None
    if cd is not None and out is None:
        n = len(cd.manifest.get("artifacts", [])) + 1
        dest = cd.subdir("uiauto") / f"screenshot-{n:03d}.png"
    else:
        dest = out or (Path.cwd() / "ui-screenshot.png")
    rc, res = ui.screenshot(str(dest))
    if (res or "").strip():
        typer.echo(res.strip())
    if rc == 0 and Path(dest).exists():
        c.success(f"Screenshot → {dest}")
        if cd is not None:
            cd.add_evidence(Path(dest), tool="agent-device", source="ui screenshot",
                            note="ui screenshot", copy=(out is not None))
            c.dim(f"  evidence → case {cd.case_id}")
    raise typer.Exit(rc)


@ui_app.command("assert", rich_help_panel="Inspect")
def ui_assert(
    needle: Annotated[str, typer.Argument(
        help="Text or element ref to check for on screen (e.g. 'Welcome' or @e2).")],
    gone: Annotated[bool, typer.Option(
        "--gone", help="Assert the target is ABSENT (inverts the check).")] = False,
    count: Annotated[Optional[int], typer.Option(
        "--count", "-n", min=0,
        help="Assert it appears EXACTLY n times (e.g. 3 list rows). 0 = absent.")] = None,
    timeout: Annotated[float, typer.Option(
        "--timeout", "-t", min=0.0,
        help="Seconds to keep re-checking until it holds (0 = check once, no wait).")] = 5.0,
    json_out: JsonOpt = False,
) -> None:
    """Assert the current screen shows (or --gone: hides) a text/ref — the
    machine-checkable verify primitive.

    Auto-settles: re-checks until the assertion holds or --timeout (default 5s)
    elapses, so a screen that renders a beat late fails nothing. A correct screen
    returns immediately — only a failing assert pays the wait.

    Exit 0 if the assertion holds, 1 if it fails, 2 on bad usage or if the screen
    can't be read at all (open a session first).
    """
    if gone and count is not None:
        c.error("Use --gone or --count, not both — '--count 0' already means absent.")
        raise typer.Exit(2)
    ui = _ui_guard()

    def _check() -> _UiCheck:
        rc, out = ui.snapshot(interactive=False)
        if rc != 0:
            return _UiCheck(ok=False, readable=False, count=0)
        n = _ui_occurrences(out or "", needle)
        holds = (n == count) if count is not None else ((n > 0) != gone)
        return _UiCheck(ok=holds, readable=True, count=n)

    res, waited = _ui_settle(_check, timeout)

    if not res.readable:
        c.error("Could not read the screen — open a session first: "
                "navig mobile ui open <app>.")
        raise typer.Exit(2)

    if json_out:
        _emit_json({"needle": needle, "present": res.count > 0, "count": res.count,
                    "gone_expected": gone, "expected_count": count,
                    "timeout": timeout, "waited_seconds": round(waited, 2),
                    "ok": res.ok})
        raise typer.Exit(0 if res.ok else 1)

    waited_note = f" (waited {waited:.1f}s)" if waited >= 0.5 else ""
    if res.ok:
        what = (f"×{res.count}" if count is not None
                else ("absent" if gone else "present"))
        c.success(f"assert OK — {needle!r} {what}.{waited_note}")
    elif count is not None:
        c.error(f"assert FAILED — {needle!r} ×{res.count}, "
                f"expected ×{count}.{waited_note}")
    else:
        c.error(f"assert FAILED — {needle!r} "
                f"{'present but expected gone' if gone else 'not found on screen'}"
                f".{waited_note}")
    raise typer.Exit(0 if res.ok else 1)


@ui_app.command("tap", rich_help_panel="Interact")
def ui_tap(
    ref: Annotated[str, typer.Argument(help="Element ref from `ui snapshot` (e.g. @e2)")],
) -> None:
    """Tap an element by its snapshot ref."""
    ui = _ui_guard()
    rc, out = ui.tap(ref)
    _ui_emit(rc, out)


@ui_app.command("fill", rich_help_panel="Interact")
def ui_fill(
    ref: Annotated[str, typer.Argument(help="Element ref (e.g. @e3)")],
    text: Annotated[str, typer.Argument(help="Text to enter into the field")],
) -> None:
    """Fill a text field identified by its snapshot ref."""
    ui = _ui_guard()
    rc, out = ui.fill(ref, text)
    _ui_emit(rc, out)


@ui_app.command("type", rich_help_panel="Interact")
def ui_type(
    text: Annotated[str, typer.Argument(help="Text to type into the focused field")],
) -> None:
    """Type text into the currently focused field."""
    ui = _ui_guard()
    rc, out = ui.type_text(text)
    _ui_emit(rc, out)


@ui_app.command("scroll", rich_help_panel="Interact")
def ui_scroll(
    direction: Annotated[Optional[str], typer.Argument(help="up | down | left | right")] = None,
) -> None:
    """Scroll the current view."""
    ui = _ui_guard()
    rc, out = ui.scroll(direction)
    _ui_emit(rc, out)


@ui_app.command("press", rich_help_panel="Interact")
def ui_press(
    key: Annotated[str, typer.Argument(help="Device button / key (e.g. back, home, enter)")],
) -> None:
    """Press a device button / key."""
    ui = _ui_guard()
    rc, out = ui.press(key)
    _ui_emit(rc, out)


@ui_app.command("wait", rich_help_panel="Interact")
def ui_wait(
    seconds: Annotated[Optional[float], typer.Argument(help="Seconds to pause (default: agent-device's own).")] = None,
) -> None:
    """Wait for the UI to settle."""
    ui = _ui_guard()
    rc, out = ui.wait(seconds)
    _ui_emit(rc, out)


@ui_app.command("record", rich_help_panel="Replay")
def ui_record(
    out: Annotated[Optional[Path], typer.Option("--out", "-o", help="Output .ad script path.")] = None,
    case: CaseOpt = None,
    platform: PlatformOpt = None,
    udid: UdidOpt = None,
) -> None:
    """Record your manual flow into a replayable .ad script (interactive)."""
    _check_platform(platform)
    ui = _ui_guard()
    _ui_require_ownership(udid, "record")
    dest = out or (Path.cwd() / "flow.ad")
    c.step(f"Recording → {dest}  (perform the flow; stop it in agent-device) …")
    rc = ui.record(str(dest), platform, udid)
    if rc == 0 and Path(dest).exists():
        c.success(f"Recorded → {dest}")
        c.dim(f"  Replay: navig mobile ui replay {dest}")
        if case is not None:
            cd = _ui_case(case, platform=platform, device=udid)
            cd.add_evidence(Path(dest), tool="agent-device", source="ui record",
                            note="recorded flow", copy=True)
            c.dim(f"  evidence → case {cd.case_id}")
    raise typer.Exit(rc)


@ui_app.command("replay", rich_help_panel="Replay")
def ui_replay(
    script: Annotated[str, typer.Argument(help="Recorded .ad script to replay")],
) -> None:
    """Replay a recorded .ad script (streams progress)."""
    if not Path(script).exists():
        c.error(f"Script not found: {script}")
        raise typer.Exit(2)
    ui = _ui_guard()
    raise typer.Exit(ui.replay(script))


@ui_app.command("exec", rich_help_panel="Replay")
def ui_exec(
    args: Annotated[Optional[list[str]], typer.Argument(
        help="Raw agent-device args (e.g. help snapshot).")] = None,
) -> None:
    """Escape hatch — pass raw arguments straight to agent-device."""
    ui = _ui_guard()
    if not args:
        c.info("Usage: navig mobile ui exec <agent-device args>  (e.g. help snapshot)")
        raise typer.Exit(0)
    raise typer.Exit(ui.passthrough(list(args)))


# ── small utils ──────────────────────────────────────────────────────────────

def _need(value, message: str) -> None:
    if not value:
        c.error(message)
        raise typer.Exit(2)


def _tri(v: Optional[bool]) -> str:
    return "yes" if v is True else ("no" if v is False else "—")


def _run_with_spinner(fn, label: str):
    # Guard only the spinner *construction* — never wrap fn() in the try, or its
    # exceptions get swallowed AND fn re-runs (double backup/acquire/flash).
    ch = c._ch
    spinner = getattr(ch, "create_spinner", None) if ch else None
    cm = None
    if callable(spinner):
        try:
            cm = spinner(label)
        except Exception:
            cm = None
    if cm is not None:
        with cm:
            return fn()
    c.step(label)
    return fn()


def _measure(path: Path) -> tuple[int, str]:
    """Return (size_bytes, sha256). sha256 only for single files; '' for dirs."""
    if path.is_file():
        h = hashlib.sha256()
        size = 0
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
                size += len(chunk)
        return size, h.hexdigest()
    if path.is_dir():
        size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        return size, ""
    return 0, ""


# ── Stage 2 helpers: consent / cases / forensics reporting ───────────────────

def _require_consent(udid: str, action: str):
    from navig_mobile.consent import ConsentGate, ConsentRequired

    try:
        return ConsentGate().require(udid, action)
    except ConsentRequired as exc:
        c.error(str(exc))
        raise typer.Exit(4) from exc


def _resolve_udid_only() -> str:
    return _resolve().udid


def _case_root(case_id: str) -> Path:
    from navig_mobile import config

    root = config.mobile_dir() / "cases" / case_id
    if not root.exists():
        c.error(f"Case {case_id!r} not found under {root.parent}.")
        raise typer.Exit(1)
    return root


def _open_or_new_case(case: Optional[str], *, udid: str, platform: str, name: str,
                      authorization_ref: str = "", scope: str = ""):
    from navig_mobile.consent import CaseDir

    if case:
        return CaseDir.open(_case_root(case))
    return CaseDir.create(udid=udid, platform=platform, name=name,
                          authorization_ref=authorization_ref, scope=scope)


def _record_case(cd) -> None:
    try:
        from navig_mobile.store import get_store

        get_store().record_case(case_id=cd.case_id, path=str(cd.root), name=cd.name,
                                udid=cd.udid, platform=cd.platform)
    except Exception:
        pass  # case index is best-effort; the on-disk manifest is authoritative


def _run_leapp_parse(plat: str, src: str, out_dir: str, input_type: Optional[str],
                     cd, json_out: bool) -> None:
    from navig_mobile.engine.forensics import leapp

    try:
        res = _run_with_spinner(
            lambda: leapp.parse(plat, src, out_dir, input_type=input_type), "parsing…")
    except leapp.LeappUnavailable as exc:
        c.error(str(exc))
        raise typer.Exit(3) from exc
    if cd is not None:
        try:
            cd.add_evidence(Path(res["output"]), tool=res["tool"],
                            source=f"case:{cd.case_id}", note="leapp report")
        except Exception:
            pass
    if json_out:
        _emit_json(res)
        return
    c.success(f"{res['tool']} parsed → {res['output']}")
    if res.get("report"):
        c.dim(f"  report: {res['report']}")
    if res["returncode"] != 0:
        c.warning(f"  parser exit {res['returncode']}: {res['stderr_tail']}")


def _print_case(cd, json_out: bool) -> None:
    m = cd.manifest
    if json_out:
        _emit_json(m)
        return
    c.print_rows(["Field", "Value"], [
        ["Case", m.get("case_id", "")],
        ["Name", m.get("name", "") or "—"],
        ["Device", m.get("udid", "") or "—"],
        ["Platform", m.get("platform", "") or "—"],
        ["Operator", m.get("operator", "") or "—"],
        ["Host", m.get("host", "") or "—"],
        ["Authorization", m.get("authorization_ref", "") or "—"],
        ["Created", (m.get("created_at", "") or "")[:19]],
    ], title=f"case {m.get('case_id', '')}")
    arts = m.get("artifacts", [])
    if arts:
        rows = [[a["evidence_id"], a["path"], (a.get("sha256") or "")[:16] + "…",
                 _human_bytes(a.get("bytes")), a.get("tool", "")] for a in arts]
        c.print_rows(["Evidence", "Path", "sha256", "Size", "Tool"], rows,
                     title=f"{len(arts)} artifact(s)")
    else:
        c.dim("  (no evidence recorded yet)")


def _print_verify(cd, json_out: bool) -> None:
    results = cd.verify()
    if json_out:
        _emit_json(results)
        return
    if not results:
        c.warning("No artifacts recorded for this case.")
        return
    rows = []
    for r in results:
        if r["ok"]:
            st = c.styled("● verified", "green")
        elif r["missing"]:
            st = c.styled("✗ MISSING", "red")
        else:
            st = c.styled("✗ MODIFIED", "red")
        rows.append([r["evidence_id"], r["path"], st])
    c.print_rows(["Evidence", "Path", "Integrity"], rows, title="chain-of-custody verify")
    bad = [r for r in results if not r["ok"]]
    (c.error if bad else c.success)(
        f"{len(results) - len(bad)}/{len(results)} artifact(s) verified"
        + (" — INTEGRITY FAILURE" if bad else "."))


def _report_scan(res: dict, case_id: Optional[str], json_out: bool) -> None:
    """Render the MVT verdict and END the command with it.

    Exit 0 means one thing only: the scan ran and found nothing (`clean` is
    ``returncode == 0 and detections == 0``). Every other outcome exits 1 —
    detections found, or MVT exiting non-zero without a parseable result, which is
    "I could not look", not "you are fine".

    It used to print all three outcomes and return, so
    ``navig mobile scan spyware --udid X && echo "device clean"`` printed *device
    clean* on a phone MVT had just flagged with mercenary-spyware indicators. On this
    surface the exit code is the whole point: the caller is a script, and the report
    it cannot read is the one that matters.
    """
    clean = bool(res.get("clean"))
    if json_out:
        # The payload still prints in full — the exit code is an extra signal, never a
        # substitute for it (a `| jq` pipeline is unaffected; a `&&` chain is not).
        _emit_json({**res, "case": case_id})
        if not clean:
            raise typer.Exit(1)
        return
    det = res.get("detections", 0)
    if clean:
        c.success(f"MVT scan clean — 0 IOC detection(s). ({res['tool']})")
    elif det > 0:
        c.error(f"⚠ MVT flagged {det} potential detection(s) — review: {res['output']}")
    else:
        c.warning(f"MVT finished (exit {res['returncode']}) — see {res['output']}. "
                  f"{res.get('stderr_tail', '')}")
    c.dim("  " + res.get("caveat", ""))
    if case_id:
        c.dim(f"  case: {case_id}  ·  evidence: navig mobile evidence {case_id}")
    # Raised AFTER the detail so the operator still sees the results path and the
    # caveat — the exit code is the verdict, not a replacement for the report.
    if not clean:
        raise typer.Exit(1)


# ── Stage 3 helpers: rooting / jailbreak ─────────────────────────────────────

def _confirm_destructive(desc: str, target: str, yes: bool) -> bool:
    """Second gate for wipe-level ops (on top of the consent record). Non-interactive:
    proceeds only with an explicit --yes."""
    c.warning(f"⚠ DESTRUCTIVE — {desc}")
    c.warning(f"  This can ERASE ALL DATA on {target} and may brick it (warranty may void).")
    if yes:
        c.dim("  Proceeding (--yes given).")
        return True
    c.error("  Refusing without --yes. Back up first (navig mobile backup create), "
            "then re-run with --yes.")
    return False


def _print_root_status(st: dict) -> None:
    c.print_rows(["Field", "Value"], [
        ["Platform", st.get("platform", "")],
        ["Rooted", _tri(st.get("rooted"))],
        ["su path", st.get("su_path") or "—"],
        ["Magisk app", _tri(st.get("magisk_app"))],
        ["Bootloader locked", _tri(st.get("bootloader_locked"))],
        ["Verified boot", st.get("verified_boot_state") or "—"],
        ["dm-verity", st.get("verity_mode") or "—"],
        ["SELinux", st.get("selinux") or "—"],
        ["Build tags", st.get("build_tags") or "—"],
        ["Security patch", st.get("security_patch") or "—"],
    ], title="root status")
    if st.get("rooted"):
        c.warning("  Device appears ROOTED.")
    elif st.get("bootloader_locked"):
        c.dim("  Bootloader locked · not rooted → `navig mobile root guide` (unlocking WIPES the device).")


def _print_root_guide() -> None:
    c.info("Android rooting with Magisk — the standard, mostly-reversible path:")
    c.plain()
    c.step("1. Back up (unlocking WIPES the device)")
    c.dim("   navig mobile backup create")
    c.step("2. Allow unlocking on-device")
    c.dim("   Settings › Developer options › enable 'OEM unlocking' + 'USB debugging'")
    c.step("3. Record authorization")
    c.dim('   navig mobile consent record -u <udid> --authorization "I own this device"')
    c.step("4. Reboot to the bootloader")
    c.dim("   navig mobile android reboot bootloader")
    c.step("5. Unlock the bootloader (ERASES ALL DATA)")
    c.dim("   navig mobile root unlock --yes")
    c.step("6. Patch your stock boot.img with the Magisk app, copy it back to the computer")
    c.step("7. Flash the patched boot image")
    c.dim("   navig mobile root flash --partition boot --image magisk_patched.img --yes")
    c.step("8. Reboot & verify")
    c.dim("   navig mobile android reboot system  →  open Magisk to confirm root")
    c.plain()
    c.warning("Needs the `fastboot` binary + your device's stock boot image. "
              "`navig mobile doctor` checks fastboot.")


def _print_jailbreak_status(st: dict) -> None:
    c.print_rows(["Field", "Value"], [
        ["Device", st.get("product_type") or "—"],
        ["iOS", st.get("os_version") or "—"],
        ["checkm8 eligible", _tri(st.get("checkm8_eligible"))],
        ["Developer mode", _tri(st.get("developer_mode"))],
        ["Recommended", st.get("recommended_tool") or "—"],
    ], title="jailbreak status")
    if st.get("eligibility"):
        c.dim("  " + st["eligibility"])


def _print_jailbreak_guide() -> None:
    c.info("iOS jailbreak — eligibility is hardware-bound (checkm8 = A11 and older):")
    c.plain()
    c.step("1. Check eligibility")
    c.dim("   navig mobile jailbreak status   (A12+ has no public jailbreak on current iOS)")
    c.step("2. Back up")
    c.dim("   navig mobile backup create")
    c.step("3. checkm8 devices → palera1n (Linux/macOS host)")
    c.dim("   Enter DFU (navig mobile ios dfu), run palera1n, follow its prompts.")
    c.step("4. Enable Developer Mode (iOS 16+)")
    c.dim("   navig mobile dev enable")
    c.plain()
    c.warning("navig-mobile does NOT run the exploit — it guides + drives DFU/recovery/dev-mode. "
              "Jailbreaking is your responsibility and can brick the device.")


# ── Stage 4 helpers: OSINT printers ──────────────────────────────────────────

def _print_identity(ident: dict) -> None:
    rows = [["Platform", ident.get("platform", "")]]
    rows += [[k, v] for k, v in ident.items() if k not in ("platform", "note") and v]
    c.print_rows(["Field", "Value"], rows, title="device identity")
    if ident.get("note"):
        c.dim("  " + ident["note"])


def _print_app_privacy(prof: dict) -> None:
    if prof.get("note"):
        c.warning(prof["note"])
        return
    dangerous = prof.get("dangerous", {})
    c.info(f"{prof['package']} — {prof.get('risk_score', 0)} high-risk permission(s) "
           f"of {prof.get('permission_count', 0)} declared")
    if dangerous:
        for perm, label in sorted(dangerous.items(), key=lambda kv: kv[1]):
            c.dim(f"  • {label}   ({perm})")
    else:
        c.success("  No high-risk permissions flagged.")


def _print_privacy_scan(scan: dict) -> None:
    apps = scan.get("apps", [])
    if scan.get("note") and not apps:
        c.warning(scan["note"])
        return
    if not apps:
        c.success(f"No apps flagged (scanned {scan.get('scanned', 0)}).")
    else:
        rows = [[a["app"], a["name"] or "—", str(a["risk_score"]),
                 ", ".join(a["grants"][:4])] for a in apps]
        c.print_rows(["App", "Name", "Risk", "Top grants"], rows,
                     title=f"{scan.get('flagged', len(apps))} flagged of {scan.get('scanned', 0)} scanned")
    if scan.get("truncated"):
        c.dim(f"  Scanned {scan.get('scanned', 0)} of {scan.get('total_user_apps', 0)} apps — "
              "raise with --limit.")


def register() -> None:  # pragma: no cover — entry-point seam; the CLI verb auto-mounts
    return None
