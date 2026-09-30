"""iOS diagnostics — crash reports, live syslog, and packet capture, driven via
the ``pymobiledevice3`` CLI. Capturing/streaming commands run interactively
(until Ctrl-C); the crash pull/ls commands are captured."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _cli() -> list[str]:
    from navig_mobile.engine.ios.device import _cli as base_cli

    return base_cli()


def _pmd3_run(args: list[str], udid: str, *, parse_json: bool = False, timeout: int = 300):
    from navig_mobile.engine.ios import device as iosdev

    return iosdev._run([*args, "--udid", udid], parse_json=parse_json, timeout=timeout)


# ── crash reports ────────────────────────────────────────────────────────────

def crash_ls(udid: str) -> str:
    return str(_pmd3_run(["crash", "ls"], udid, timeout=60))


def crash_pull(udid: str, out_dir: str) -> str:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    _pmd3_run(["crash", "pull", str(out_dir)], udid, timeout=300)
    return str(out_dir)


# ── streaming commands (interactive — build argv purely so it's testable) ────

def syslog_argv(udid: str) -> list[str]:
    return [*_cli(), "syslog", "live", "--udid", udid]


def pcap_argv(udid: str, out_file: str) -> list[str]:
    return [*_cli(), "pcap", str(out_file), "--udid", udid]


def dvt_argv(udid: str, args: list[str]) -> list[str]:
    return [*_cli(), "developer", "dvt", *args, "--udid", udid]


def dvt(udid: str, args: list[str]) -> int:
    return subprocess.run(dvt_argv(udid, args)).returncode


def syslog_live(udid: str) -> int:
    return subprocess.run(syslog_argv(udid)).returncode


def pcap(udid: str, out_file: str) -> int:
    Path(out_file).parent.mkdir(parents=True, exist_ok=True)
    return subprocess.run(pcap_argv(udid, out_file)).returncode
