"""Local OBS health checks for `navig obs doctor` — pure filesystem/process probes.

Everything here runs WITHOUT a connection to OBS: install detection, websocket
config state, plugin binary consistency, the newest OBS log's load errors, and
scene-collection file references that no longer exist (the "Missing Files"
dialog). No network, no writes — safe against a live or closed OBS.
"""

from __future__ import annotations

import json
import os
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from navig_obs.engine.config import DEFAULT_PORT, local_obs_websocket_config

# check states: pass < warn < fail
PASS = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class Check:
    name: str
    state: str
    detail: str = ""
    hint: str = ""


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)
    missing_files: list[dict[str, str]] = field(default_factory=list)
    log_errors: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(c.state == FAIL for c in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "checks": [vars(c) for c in self.checks],
            "missing_files": self.missing_files,
            "log_errors": self.log_errors,
            "failed": self.failed,
        }


# ── locations ────────────────────────────────────────────────────────────────
def obs_install_dir() -> Path | None:
    """Where obs64.exe lives (Windows-first; mac/linux common paths probed)."""
    candidates: list[Path] = []
    pf = os.environ.get("ProgramFiles")
    if pf:
        candidates.append(Path(pf) / "obs-studio")
    pf86 = os.environ.get("ProgramFiles(x86)")
    if pf86:
        candidates.append(Path(pf86) / "obs-studio")
    candidates.append(Path("/Applications/OBS.app"))
    for c in candidates:
        if (c / "bin" / "64bit" / "obs64.exe").exists() or (c / "bin" / "64bit" / "obs").exists():
            return c
    # Linux package installs (bin directly under /usr/bin)
    for exe in ("/usr/bin/obs", "/usr/bin/obs-studio"):
        if Path(exe).exists():
            return Path("/usr")
    return None


def obs_config_dir() -> Path | None:
    """OBS config dir (%APPDATA%\\obs-studio / ~/Library/... / ~/.config/obs-studio)."""
    appdata = os.environ.get("APPDATA")
    candidates: list[Path] = []
    if appdata:
        candidates.append(Path(appdata) / "obs-studio")
    candidates.append(Path.home() / "Library" / "Application Support" / "obs-studio")
    candidates.append(Path.home() / ".config" / "obs-studio")
    for c in candidates:
        if c.exists():
            return c
    return None


def probe_port(host: str, port: int, timeout: float = 0.6) -> bool:
    """True when something accepts TCP on host:port (OBS running + ws enabled?)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ── checks ───────────────────────────────────────────────────────────────────
def check_install(report: DoctorReport) -> Path | None:
    install = obs_install_dir()
    if install is None:
        report.checks.append(
            Check("OBS install", FAIL, "not found", "install OBS Studio or pass the path")
        )
        return None
    version = ""
    exe = install / "bin" / "64bit" / "obs64.exe"
    if exe.exists():
        try:
            import time as _time

            version = f" (obs64.exe built {_time.strftime('%Y-%m-%d', _time.localtime(exe.stat().st_mtime))})"
        except OSError:
            pass
    report.checks.append(Check("OBS install", PASS, str(install) + version))
    return install


def check_config_and_socket(report: DoctorReport, host: str = "127.0.0.1") -> dict | None:
    """Websocket config state + a live port probe. Returns the config dict."""
    config_dir = obs_config_dir()
    if config_dir is None:
        report.checks.append(
            Check("OBS config", WARN, "config dir not found — start OBS once to create it")
        )
        return None
    report.checks.append(Check("OBS config", PASS, str(config_dir)))

    ws_cfg = local_obs_websocket_config()
    if ws_cfg is None:
        report.checks.append(
            Check(
                "websocket server",
                WARN,
                "no obs-websocket config",
                "in OBS: Tools → WebSocket Server Settings → enable",
            )
        )
    else:
        port = int(ws_cfg.get("server_port") or DEFAULT_PORT)
        enabled = bool(ws_cfg.get("server_enabled"))
        auth = bool(ws_cfg.get("auth_required"))
        if not enabled:
            report.checks.append(
                Check(
                    "websocket server",
                    WARN,
                    f"DISABLED (port {port})",
                    "in OBS: Tools → WebSocket Server Settings → 'Enable WebSocket server'",
                )
            )
        elif probe_port(host, port):
            report.checks.append(
                Check("websocket server", PASS, f"enabled on {host}:{port}, OBS is running")
            )
        else:
            report.checks.append(
                Check(
                    "websocket server",
                    WARN,
                    f"enabled on port {port} but nothing is listening",
                    "start OBS",
                )
            )
        if auth:
            pwd_ok = bool(ws_cfg.get("server_password"))
            report.checks.append(
                Check(
                    "websocket auth",
                    PASS if pwd_ok else WARN,
                    "password required" + ("" if pwd_ok else " but no password set"),
                    "" if pwd_ok else "set a password in OBS or NAVIG_OBS_PASSWORD will not match",
                )
            )
    return ws_cfg


def read_newest_log(config_dir: Path | None = None) -> tuple[str | None, str]:
    """Return (name, text) of the newest OBS log, or (None, "")."""
    cfg = config_dir or obs_config_dir()
    if cfg is None:
        return None, ""
    logs = cfg / "logs"
    if not logs.exists():
        return None, ""
    # Windows OBS names logs "YYYY-MM-DD HH-MM-SS.txt"; Linux/macOS use .log.
    candidates = list(logs.glob("*.log")) + list(logs.glob("*.txt"))
    if not candidates:
        return None, ""
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        return newest.name, newest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return newest.name, ""


_LOAD_FAILURE = re.compile(
    r"(LoadLibrary failed for|Failed to load module file)", re.IGNORECASE
)
_DLL_IN_QUOTES = re.compile(r"'([^']+\.dll)'", re.IGNORECASE)


def _load_failures(log_text: str) -> dict[str, str]:
    """Map plugin-dll stem (lower) → the log line proving OBS couldn't load it.

    Covers both shapes: ``LoadLibrary failed for '…\\StreamFX.dll': …`` (the DLL
    exists but a dependency is missing — error 126) and ``Failed to load module
    file '…\\source-copy.dll', module failed to open`` (stale/incompatible).
    """
    out: dict[str, str] = {}
    for line in log_text.splitlines():
        if not _LOAD_FAILURE.search(line):
            continue
        m = _DLL_IN_QUOTES.search(line) or re.search(r"([\w-]+\.dll)", line, re.IGNORECASE)
        if m:
            out[Path(m.group(1)).stem.lower()] = line.strip()
    return out


def check_plugins(
    report: DoctorReport,
    install: Path | None,
    config_dir: Path | None,
    log_text: str = "",
) -> None:
    """plugin_config entries vs actual plugin binaries (the 'failed to load' dialog).

    A binary merely EXISTING is not health: when the OBS log proves the module
    failed to load, the verdict downgrades to WARN (StreamFX ships its DLL yet
    LoadLibrary still fails on a missing dependency).
    """
    if config_dir is None:
        return
    pc = config_dir / "plugin_config"
    failures = _load_failures(log_text)
    covered: set[str] = set()

    search_roots: list[Path] = []
    if install is not None:
        search_roots += [install / "obs-plugins" / "64bit", install / "obs-plugins"]
    search_roots.append(config_dir / "plugins")
    appdata = os.environ.get("APPDATA")
    if appdata:
        search_roots.append(Path(appdata) / "obs-studio" / "plugins")
    # StreamFX and friends install here by default (the OBS log names this path).
    search_roots.append(Path(os.environ.get("ProgramData", "C:/ProgramData")) / "obs-studio" / "plugins")

    if pc.exists():
        for entry in sorted(p for p in pc.iterdir() if p.is_dir()):
            name = entry.name
            if name in ("obs-websocket",):  # bundled with OBS itself
                continue
            covered.add(name.lower())
            found: Path | None = None
            for root in search_roots:
                if not root.exists():
                    continue
                for pattern in (f"{name}.dll", f"{name}.so", f"{name}.dylib", f"obs-{name}.dll"):
                    hit = next(root.rglob(pattern), None) if root.is_dir() else None
                    if hit:
                        found = hit
                        break
                if found:
                    break
            if found is None:
                report.checks.append(
                    Check(
                        f"plugin {name}",
                        WARN,
                        "config present but plugin binary NOT FOUND",
                        "reinstall the plugin or remove its config folder",
                    )
                )
                continue
            if name.lower() in failures:
                report.checks.append(
                    Check(
                        f"plugin {name}",
                        WARN,
                        f"binary found ({found.name}) but OBS failed to load it",
                        "usually a missing dependency or an outdated plugin — update or remove it",
                    )
                )
                continue
            try:
                mtime = found.stat().st_mtime
                import time as _time

                age = _time.strftime("%Y-%m-%d", _time.localtime(mtime))
                report.checks.append(Check(f"plugin {name}", PASS, f"{found.name} (built {age})"))
            except OSError:
                report.checks.append(Check(f"plugin {name}", PASS, found.name))

    # Load failures for plugins that have NO plugin_config entry (source-copy
    # style: a bare DLL the config-driven loop never sees).
    for stem, line in sorted(failures.items()):
        if stem in covered or stem.startswith(("obs", "enc-", "decklink")):
            continue
        report.checks.append(
            Check(
                f"plugin {stem}",
                WARN,
                "OBS log: module failed to load",
                line[:200],
            )
        )


def check_log_errors(report: DoctorReport, log_name: str | None, log_text: str, limit: int = 40) -> None:
    """Lines from the newest OBS log that look like load/runtime errors."""
    if log_name is None:
        return
    if not log_text:
        report.checks.append(Check("OBS log", WARN, f"unreadable ({log_name})"))
        return
    needles = ("error", "failed to load", "cannot load", "module failed", "not found", "traceback")
    hits = [
        line.strip()
        for line in log_text.splitlines()
        if any(n in line.lower() for n in needles) and "no errors" not in line.lower()
    ]
    report.log_errors = hits[:limit]
    if hits:
        report.checks.append(
            Check(
                "OBS log",
                WARN,
                f"{len(hits)} error-like line(s) in {log_name} (see below)",
            )
        )
    else:
        report.checks.append(Check("OBS log", PASS, f"no error lines in {log_name}"))


# ── scene collections: the "Missing Files" dialog ────────────────────────────
_FILE_KEYS = ("file", "local_file")
_SKIP_PREFIXES = ("http://", "https://", "rtmp://", "rtmps://", "obs://", "<")


def scene_collections(config_dir: Path | None = None) -> list[Path]:
    cfg = config_dir or obs_config_dir()
    if cfg is None:
        return []
    scenes = cfg / "basic" / "scenes"
    if not scenes.exists():
        return []
    return sorted(scenes.glob("*.json"))  # excludes OBS's *.bak sidecars


def missing_scene_files(report: DoctorReport, config_dir: Path | None = None) -> None:
    """Scan every scene collection for file references that no longer exist.

    Walks the JSON tree; a ``file``/``local_file`` string setting is checked on
    disk. The nearest ancestor ``name`` (the source's name) travels along, so a
    row reads like the OBS Missing Files dialog: source → path.
    """
    for collection in scene_collections(config_dir):
        try:
            data = json.loads(collection.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report.checks.append(
                Check(f"collection {collection.name}", WARN, "unreadable JSON")
            )
            continue
        seen: set[str] = set()
        for source_name, file_path in _walk_files(data):
            if file_path in seen:
                continue
            seen.add(file_path)
            try:
                exists = Path(file_path).exists()
            except OSError:
                exists = False
            if not exists:
                report.missing_files.append(
                    {
                        "collection": collection.stem,
                        "source": source_name or "?",
                        "file": file_path,
                    }
                )


def _walk_files(node: Any, inherited_name: str = "") -> list[tuple[str, str]]:
    """Yield (source_name, file_path) for every file-ish setting in the tree."""
    out: list[tuple[str, str]] = []
    if isinstance(node, dict):
        name = node.get("name") if isinstance(node.get("name"), str) else inherited_name
        for key, value in node.items():
            if key in _FILE_KEYS and isinstance(value, str) and value.strip():
                if not value.startswith(_SKIP_PREFIXES):
                    out.append((name, value))
            else:
                out.extend(_walk_files(value, name))
    elif isinstance(node, list):
        for item in node:
            out.extend(_walk_files(item, inherited_name))
    return out


def run_doctor(host: str = "127.0.0.1") -> DoctorReport:
    """Run every local check and return the consolidated report."""
    report = DoctorReport()
    install = check_install(report)
    check_config_and_socket(report, host=host)
    log_name, log_text = read_newest_log()
    check_plugins(report, install, obs_config_dir(), log_text)
    check_log_errors(report, log_name, log_text)
    missing_scene_files(report)
    if report.missing_files:
        report.checks.append(
            Check(
                "scene file references",
                WARN,
                f"{len(report.missing_files)} missing file(s) across "
                f"{len({m['collection'] for m in report.missing_files})} collection(s)",
            )
        )
    else:
        report.checks.append(Check("scene file references", PASS, "all referenced files exist"))
    return report
