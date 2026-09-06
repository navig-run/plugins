"""Consent-gated logical acquisition into a case directory.

- **iOS**: a full ``mobilebackup2`` backup (via the iOS engine) → the case's
  ``acquisition/`` dir. This is what MVT's ``check-backup`` consumes.
- **Android**: an ``adb bugreport`` zip plus a lightweight device/property/package
  snapshot. This is what MVT's ``check-bugreport`` consumes.

Each produced artifact is hashed into the case manifest (chain of custody).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from navig_mobile.consent import CaseDir
from navig_mobile.engine.base import Device, DeviceError, Platform, ProgressCb


def _adb_bin() -> str | None:
    return shutil.which("adb")


def _run_adb(args: list[str], *, timeout: int = 1800) -> int:
    """Run the adb binary. Isolated for test monkeypatching."""
    adb = _adb_bin()
    if not adb:
        raise DeviceError("`adb` binary required for Android acquisition — see `navig mobile doctor`.")
    return subprocess.run([adb, *args], timeout=timeout).returncode


def acquire(device: Device, case: CaseDir, *, progress: ProgressCb | None = None) -> dict[str, Any]:
    """Logically acquire ``device`` into ``case``. Returns a summary dict."""
    acq = case.subdir("acquisition")
    if device.platform is Platform.IOS:
        return _acquire_ios(device, case, acq, progress)
    return _acquire_android(device, case, acq, progress)


def _acquire_ios(device: Device, case: CaseDir, acq: Path,
                 progress: ProgressCb | None) -> dict[str, Any]:
    if progress:
        progress(0, 0, "iOS: creating mobilebackup2 backup (this can take a while)…")
    dest = acq / "backup"
    path = device.backup(str(dest), progress=progress)
    entry = case.add_evidence(Path(path), tool=_ios_tool(), source=f"device:{device.udid}",
                              note="mobilebackup2 full backup")
    case.log_event("acquired", f"ios backup → {entry['path']}")
    return {"type": "backup", "platform": "ios", "path": str(path),
            "evidence": entry, "mvt_command": "check-backup"}


def _acquire_android(device: Device, case: CaseDir, acq: Path,
                     progress: ProgressCb | None) -> dict[str, Any]:
    # 1) cheap device snapshot (props + package list) via the adb shell we already hold
    raw = getattr(device, "_raw", None)
    if raw is not None:
        try:
            (acq / "getprop.txt").write_text(raw.shell("getprop"), encoding="utf-8", errors="replace")
            (acq / "packages.txt").write_text(raw.shell("pm list packages -f"),
                                              encoding="utf-8", errors="replace")
            for f in ("getprop.txt", "packages.txt"):
                case.add_evidence(acq / f, tool="adb shell", source=f"device:{device.udid}")
        except Exception:
            pass  # snapshot is best-effort; the bugreport is the primary artifact
    # 2) the bugreport (primary MVT input)
    if progress:
        progress(0, 0, "Android: capturing adb bugreport (this can take a few minutes)…")
    zip_path = acq / "bugreport.zip"
    rc = _run_adb(["-s", device.udid, "bugreport", str(zip_path)], timeout=1800)
    produced = _resolve_bugreport(acq, zip_path)
    if rc != 0 or produced is None:
        raise DeviceError(f"adb bugreport failed (exit {rc}).")
    entry = case.add_evidence(produced, tool="adb bugreport", source=f"device:{device.udid}",
                              note="android bugreport")
    case.log_event("acquired", f"android bugreport → {entry['path']}")
    return {"type": "bugreport", "platform": "android", "path": str(produced),
            "evidence": entry, "mvt_command": "check-bugreport"}


def _resolve_bugreport(acq: Path, expected: Path) -> Path | None:
    if expected.exists():
        return expected
    # some adb versions name the file themselves (bugreport-<device>-<date>.zip)
    zips = sorted(acq.glob("bugreport*.zip"))
    return zips[-1] if zips else None


def _ios_tool() -> str:
    try:
        from importlib.metadata import version

        return f"pymobiledevice3 {version('pymobiledevice3')}"
    except Exception:
        return "pymobiledevice3"
