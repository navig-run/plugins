"""Spyware scanning via MVT (Amnesty Mobile Verification Toolkit).

MVT is **detected + invoked as a subprocess, never imported** (its license is
use-restricted). Current MVT CLI (verified against the source):

  * iOS      → ``mvt-ios check-backup BACKUP_PATH -o <out> [--iocs <stix2>]``
  * Android  → ``mvt-android check-bugreport BUGREPORT -o <out> [--iocs <stix2>]``
    (note: ``mvt-android check-adb`` was **removed** upstream — the live-device
    path is a bugreport/AndroidQF acquisition, then check-bugreport.)

Public indicators of compromise are necessary-but-not-sufficient: a clean MVT run
does not prove a device is uncompromised. We surface that honestly.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any
from navig.core.proc_text import decode_console_result


class MvtUnavailable(RuntimeError):
    def __init__(self, platform: str):
        self.platform = platform
        super().__init__(
            f"MVT ({'mvt-ios' if platform == 'ios' else 'mvt-android'}) not found. "
            "Install it (pip install mvt) — note MVT's license is use-restricted; "
            "review it before use. Then re-run. See `navig mobile doctor`."
        )


def find_mvt(platform: str) -> str | None:
    return shutil.which("mvt-ios" if platform == "ios" else "mvt-android")


def _run_mvt(argv: list[str], *, timeout: int = 1800) -> tuple[int, str, str]:
    """Run an MVT command. Isolated for test monkeypatching."""
    proc = decode_console_result(subprocess.run(argv, capture_output=True, timeout=timeout))
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def scan(platform: str, acquisition_path: str, out_dir: str, *,
         iocs: str | None = None) -> dict[str, Any]:
    """Run the platform's MVT check against an acquisition; results → ``out_dir``."""
    exe = find_mvt(platform)
    if not exe:
        raise MvtUnavailable(platform)
    cmd = "check-backup" if platform == "ios" else "check-bugreport"
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    argv = [exe, cmd, "--output", str(out_dir)]
    if iocs:
        argv += ["--iocs", iocs]
    argv.append(str(acquisition_path))
    rc, out, err = _run_mvt(argv)
    detections = _count_detections(Path(out_dir))
    return {
        "tool": f"{'mvt-ios' if platform == 'ios' else 'mvt-android'} {cmd}",
        "output": str(out_dir),
        "returncode": rc,
        "detections": detections,
        "clean": rc == 0 and detections == 0,
        "stderr_tail": (err.strip().splitlines() or [""])[-1],
        "caveat": "Public IOCs are necessary but not sufficient — a clean scan is "
                  "not proof a device is uncompromised.",
    }


def check_iocs(platform: str, results_dir: str, iocs: str) -> dict[str, Any]:
    """Re-check an existing MVT results folder against a STIX2 IOC feed."""
    exe = find_mvt(platform)
    if not exe:
        raise MvtUnavailable(platform)
    argv = [exe, "check-iocs", "--iocs", iocs, str(results_dir)]
    rc, out, err = _run_mvt(argv, timeout=600)
    return {"returncode": rc, "detections": _count_detections(Path(results_dir)),
            "stdout_tail": (out.strip().splitlines() or [""])[-1]}


def _count_detections(out_dir: Path) -> int:
    """MVT writes ``*_detected.json`` files when IOCs match — count their entries."""
    total = 0
    if not out_dir.exists():
        return 0
    for f in out_dir.rglob("*_detected.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            total += len(data) if isinstance(data, list) else 1
        except Exception:
            total += 1  # a detection file exists even if unparsable
    return total
