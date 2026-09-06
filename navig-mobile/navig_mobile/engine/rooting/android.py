"""Android rooting assist — read-only state detection + fastboot drivers.

Detection is via ``getprop`` / ``pm`` / ``getenforce`` over the adb shell we
already hold (no root required). Bootloader operations shell out to the
``fastboot`` binary (detected; guided if absent). Nothing here executes a
destructive action on its own — the command layer double-gates unlock/flash.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

from navig.core.proc_text import decode_console_result
from navig_mobile.engine.base import DeviceError


def root_status(device: Any) -> dict[str, Any]:
    """Read-only root / bootloader / boot-integrity status for an Android device."""
    raw = getattr(device, "_raw", None)
    if raw is None:
        raise DeviceError("root status is Android-only.")

    def prop(name: str) -> str:
        try:
            return (raw.getprop(name) or "").strip()
        except Exception:
            return ""

    def shell(cmd: str) -> str:
        try:
            return (raw.shell(cmd) or "").strip()
        except Exception:
            return ""

    su = shell("which su")
    magisk_app = "com.topjohnwu.magisk" in shell("pm list packages com.topjohnwu.magisk")
    selinux = shell("getenforce")
    locked = prop("ro.boot.flash.locked")          # "1" locked / "0" unlocked
    vbstate = prop("ro.boot.verifiedbootstate")    # green/yellow/orange/red
    verity = prop("ro.boot.veritymode")
    tags = prop("ro.build.tags")
    security_patch = prop("ro.build.version.security_patch")

    rooted = bool(su) or magisk_app or tags == "test-keys"
    bootloader_locked: bool | None = None
    if locked in ("0", "1"):
        bootloader_locked = locked == "1"
    elif vbstate:
        bootloader_locked = vbstate.lower() == "green"

    return {
        "platform": "android",
        "rooted": rooted,
        "su_path": su or None,
        "magisk_app": magisk_app,
        "selinux": selinux or None,
        "bootloader_locked": bootloader_locked,
        "verified_boot_state": vbstate or None,
        "verity_mode": verity or None,
        "build_tags": tags or None,
        "security_patch": security_patch or None,
    }


# ── fastboot drivers ─────────────────────────────────────────────────────────

def find_fastboot() -> str | None:
    return shutil.which("fastboot")


def _run_fastboot(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
    """Run the fastboot binary. Isolated for test monkeypatching."""
    fb = find_fastboot()
    if not fb:
        raise DeviceError(
            "`fastboot` not found — install Android platform-tools "
            "(see `navig mobile doctor`)."
        )
    proc = decode_console_result(subprocess.run([fb, *args], capture_output=True, timeout=timeout))
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def fastboot_devices() -> list[str]:
    """Serials of devices currently in fastboot/bootloader mode ([] if none/absent)."""
    try:
        rc, out, _ = _run_fastboot(["devices"], timeout=15)
    except DeviceError:
        return []
    return [line.split()[0] for line in out.splitlines() if line.strip()]


def unlock(serial: str | None = None) -> tuple[int, str]:
    """`fastboot flashing unlock` — DESTRUCTIVE (wipes the device)."""
    args = (["-s", serial] if serial else []) + ["flashing", "unlock"]
    rc, out, err = _run_fastboot(args, timeout=180)
    return rc, (out + err).strip()


def flash(partition: str, image: str, serial: str | None = None) -> tuple[int, str]:
    """`fastboot flash <partition> <image>` — DESTRUCTIVE."""
    args = (["-s", serial] if serial else []) + ["flash", partition, image]
    rc, out, err = _run_fastboot(args, timeout=600)
    return rc, (out + err).strip()


def sideload(zip_path: str, serial: str | None = None) -> tuple[int, str]:
    """`adb sideload <zip>` — flash an OTA/Magisk zip (device must be in recovery)."""
    adb = shutil.which("adb")
    if not adb:
        raise DeviceError("`adb` binary required for sideload (see `navig mobile doctor`).")
    args = (["-s", serial] if serial else []) + ["sideload", zip_path]
    proc = decode_console_result(subprocess.run([adb, *args], capture_output=True, timeout=1200))
    return proc.returncode, (proc.stdout + proc.stderr).strip()
