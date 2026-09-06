"""Forensic artifact parsing via iLEAPP (iOS) / ALEAPP (Android).

Detected + invoked as subprocesses (MIT-licensed, but not bundled). These tools
ship no reliable console-script, so we locate them via ``which`` or a configured
path (``mobile.ileapp_cmd`` / ``mobile.aleapp_cmd``) and invoke:

    <tool> -t <input_type> -i <input_path> -o <out_dir>

input types: iOS = fs|tar|zip|gz|itunes|file ; Android = fs|tar|zip|gz.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from navig.core.proc_text import decode_console_result
from navig_mobile import config


class LeappUnavailable(RuntimeError):
    def __init__(self, platform: str):
        tool = "iLEAPP" if platform == "ios" else "ALEAPP"
        pkg = "ileapp" if platform == "ios" else "aleapp"
        self.platform = platform
        super().__init__(
            f"{tool} not found. Install it (pip install {pkg}) or point navig-mobile "
            f"at it: `navig config set plugins.mobile.{pkg}_cmd \"python C:/path/{pkg}.py\"`. "
            f"See `navig mobile doctor`."
        )


def find_leapp(platform: str) -> list[str] | None:
    """Return an argv prefix for the parser, or None if not found."""
    key = "ileapp" if platform == "ios" else "aleapp"
    configured = config.get(f"{key}_cmd")
    if configured:
        return str(configured).split()
    for name in ((f"{key}", f"{key}GUI")):
        hit = shutil.which(name)
        if hit:
            return [hit]
    return None


def _run_leapp(argv: list[str], *, timeout: int = 1800) -> tuple[int, str, str]:
    """Run a LEAPP parser. Isolated for test monkeypatching."""
    proc = decode_console_result(subprocess.run(argv, capture_output=True, timeout=timeout))
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _default_input_type(platform: str, input_path: str) -> str:
    p = Path(input_path)
    if p.is_dir():
        # An iOS mobilebackup2 dir is an iTunes-format backup.
        return "itunes" if platform == "ios" else "fs"
    suffix = p.suffix.lower().lstrip(".")
    return suffix if suffix in ("tar", "zip", "gz") else ("file" if platform == "ios" else "fs")


def parse(platform: str, input_path: str, out_dir: str, *,
          input_type: str | None = None) -> dict[str, Any]:
    prefix = find_leapp(platform)
    if not prefix:
        raise LeappUnavailable(platform)
    itype = input_type or _default_input_type(platform, input_path)
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    argv = [*prefix, "-t", itype, "-i", str(input_path), "-o", str(out_dir)]
    rc, out, err = _run_leapp(argv)
    report = _find_report(Path(out_dir))
    return {"tool": ("iLEAPP" if platform == "ios" else "ALEAPP"),
            "input_type": itype, "output": str(out_dir), "returncode": rc,
            "report": str(report) if report else "",
            "stderr_tail": (err.strip().splitlines() or [""])[-1]}


def _find_report(out_dir: Path) -> Path | None:
    if not out_dir.exists():
        return None
    hits = sorted(out_dir.rglob("index.html")) or sorted(out_dir.rglob("*.html"))
    return hits[0] if hits else None
