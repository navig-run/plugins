"""On-demand system malware scan.

Primary engine: **Windows Defender** (``MpCmdRun.exe``) — the one AV on Windows
with a reliable, documented scan CLI. Malwarebytes is detected and reported, but
consumer Malwarebytes ships **no headless scan CLI**, so it can't be driven
programmatically; we surface that honestly and fall back to Defender.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_WIN = sys.platform.startswith("win")


def find_defender() -> Path | None:
    """Locate MpCmdRun.exe (prefer the versioned Platform build, else Program Files)."""
    if not _WIN:
        return None
    import os
    pf = Path(os.environ.get("ProgramData", r"C:\ProgramData"))
    plat = pf / "Microsoft" / "Windows Defender" / "Platform"
    if plat.exists():
        builds = sorted((d for d in plat.iterdir() if d.is_dir()), reverse=True)
        for d in builds:
            exe = d / "MpCmdRun.exe"
            if exe.exists():
                return exe
    legacy = Path(r"C:\Program Files\Windows Defender\MpCmdRun.exe")
    return legacy if legacy.exists() else None


@dataclass
class AVStatus:
    defender: Path | None
    malwarebytes_installed: bool
    malwarebytes_scriptable: bool
    note: str


def detect() -> AVStatus:
    """Report which system AV engines are usable for a scan."""
    defender = find_defender()
    mb_installed = mb_scriptable = False
    note = ""
    if _WIN:
        mb_root = Path(r"C:\Program Files\Malwarebytes")
        mb_installed = mb_root.exists()
        # A real Malwarebytes *scanner* CLI would be mbam.exe/mbamservice/mbcmd.
        for cand in ("Anti-Malware/mbam.exe", "Anti-Malware/MBAMService.exe", "mbcmd.exe"):
            if (mb_root / cand).exists():
                mb_scriptable = True
                break
        if mb_installed and not mb_scriptable:
            note = ("Malwarebytes is present but exposes no headless scan CLI "
                    "(consumer builds never do) — using Windows Defender instead.")
        elif not defender:
            note = "Windows Defender MpCmdRun.exe not found."
    else:
        note = "System scan uses Windows Defender and is Windows-only."
    return AVStatus(defender, mb_installed, mb_scriptable, note)


_SCAN_TYPE = {"quick": "1", "full": "2", "custom": "3"}


def run_defender_scan(scan_type: str = "quick", path: str | None = None,
                      timeout: int | None = None) -> int:
    """Kick off a Windows Defender scan, streaming output. Returns the exit code
    (0 = clean/completed, 2 = threats found by MpCmdRun convention, 127 = unavailable)."""
    exe = find_defender()
    if not exe:
        return 127
    argv = [str(exe), "-Scan", "-ScanType", _SCAN_TYPE.get(scan_type, "1")]
    if scan_type == "custom":
        if not path:
            raise ValueError("custom scan requires a path")
        argv += ["-File", str(path)]
    try:
        return subprocess.run(argv, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        return 124
