"""agent-device wrapper — detect + drive the ``agent-device`` CLI as a subprocess.

`agent-device` (callstack, MIT — https://github.com/callstack/agent-device) is a
Node CLI that drives app UIs for agentic verification: accessibility snapshots with
element refs (``@e3``), tap/fill/type/scroll/press, screenshots + evidence capture,
and replayable ``.ad`` scripts, across iOS / Android / web / desktop.

It is **detected + invoked as a subprocess**, never imported/bundled — it's a Node
package (needs Node 22+), and this keeps navig-mobile pure-Python + Apache-2.0
clean. Mirrors ``engine.devtools.frida``: a capturing ``_run`` (isolated for test
monkeypatching) plus thin typed verb wrappers.

agent-device is **session-oriented**: ``open`` starts a session bound to a
platform/device, then ``snapshot``/``tap``/``fill``/… operate on that live session
until ``close``. So the platform/device flags are threaded through the *entry*
verbs (open/apps/devices/launch/record); the in-session verbs need none.
"""

from __future__ import annotations

import shutil
import subprocess
from navig.core.proc_text import decode_console_result

_EXE_NAMES = ["agent-device"]  # npm global bin (agent-device.cmd on Windows via PATHEXT)

# Platforms agent-device targets (its --platform values).
PLATFORMS = ("ios", "android", "web", "tvos", "macos", "linux")

INSTALL_HINT = (
    "npm install -g agent-device@latest  (needs Node 22+; 24+ for web). "
    "Then: agent-device doctor"
)


class AgentDeviceUnavailable(RuntimeError):
    """The ``agent-device`` CLI isn't on PATH."""

    def __init__(self) -> None:
        super().__init__(
            "agent-device not found — install it to drive/verify app UIs "
            f"({INSTALL_HINT}). See `navig mobile doctor`."
        )


# ── detection ────────────────────────────────────────────────────────────────

def _exe() -> str | None:
    for name in _EXE_NAMES:
        hit = shutil.which(name)
        if hit:
            return hit
    return None


def available() -> bool:
    return _exe() is not None


def version() -> str:
    """``agent-device --version`` (best-effort; '' if unavailable)."""
    exe = _exe()
    if not exe:
        return ""
    try:
        proc = decode_console_result(subprocess.run([exe, "--version"], capture_output=True, timeout=15))
        return (proc.stdout or proc.stderr or "").strip().splitlines()[0].strip() if (
            proc.stdout or proc.stderr) else ""
    except Exception:
        return ""


def _require() -> str:
    exe = _exe()
    if not exe:
        raise AgentDeviceUnavailable()
    return exe


# ── runners ──────────────────────────────────────────────────────────────────

def _run(args: list[str], *, timeout: int = 120) -> tuple[int, str]:
    """Run a capturing agent-device subcommand. Isolated for test monkeypatching.

    Returns ``(returncode, combined_stdout+stderr)``. Raises
    :class:`AgentDeviceUnavailable` if the CLI isn't installed.
    """
    exe = _require()
    proc = decode_console_result(subprocess.run([exe, *args], capture_output=True, timeout=timeout))
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _run_interactive(args: list[str]) -> int:
    """Run an agent-device subcommand with inherited stdio (streaming/interactive —
    e.g. ``record`` capturing your manual actions, or a long ``replay``)."""
    exe = _require()
    return subprocess.run([exe, *args]).returncode


def _platform_args(platform: str | None, device: str | None) -> list[str]:
    args: list[str] = []
    if platform:
        args += ["--platform", platform]
    if device:
        # agent-device accepts --device <name> or --udid <id>; --device is the
        # general selector (a udid is a valid device value).
        args += ["--device", device]
    return args


# ── discovery / session (entry verbs carry platform/device) ──────────────────

def doctor() -> tuple[int, str]:
    """agent-device's own environment check (SDKs, drivers, permissions)."""
    return _run(["doctor"], timeout=90)


def list_apps(platform: str | None = None, device: str | None = None,
              *, json_out: bool = False) -> tuple[int, str]:
    args = ["apps", *_platform_args(platform, device)]
    if json_out:
        args.append("--json")
    return _run(args)


def list_devices(*, json_out: bool = False) -> tuple[int, str]:
    args = ["devices"]
    if json_out:
        args.append("--json")
    return _run(args)


def open_app(app: str, platform: str | None = None, device: str | None = None) -> tuple[int, str]:
    return _run(["open", app, *_platform_args(platform, device)])


def launch(app: str | None = None, platform: str | None = None,
           device: str | None = None) -> tuple[int, str]:
    args = ["launch"] + ([app] if app else []) + _platform_args(platform, device)
    return _run(args)


def close() -> tuple[int, str]:
    return _run(["close"])


# ── inspection (operate on the open session) ─────────────────────────────────

def snapshot(*, interactive: bool = True, json_out: bool = False) -> tuple[int, str]:
    """Accessibility snapshot with element refs (``@e1``…). ``interactive`` (-i)
    returns only actionable elements — the usual agent input."""
    args = ["snapshot"]
    if interactive:
        args.append("-i")
    if json_out:
        args.append("--json")
    return _run(args)


def screenshot(path: str) -> tuple[int, str]:
    return _run(["screenshot", path])


# ── interaction (operate on the open session) ────────────────────────────────

def tap(ref: str) -> tuple[int, str]:
    return _run(["tap", ref])


def fill(ref: str, text: str) -> tuple[int, str]:
    return _run(["fill", ref, text])


def type_text(text: str) -> tuple[int, str]:
    return _run(["type", text])


def scroll(direction: str | None = None) -> tuple[int, str]:
    return _run(["scroll"] + ([direction] if direction else []))


def press(key: str) -> tuple[int, str]:
    return _run(["press", key])


def wait(seconds: float | None = None) -> tuple[int, str]:
    return _run(["wait"] + ([str(seconds)] if seconds is not None else []))


# ── evidence / replay (streaming) ────────────────────────────────────────────

def record(out: str | None = None, platform: str | None = None,
           device: str | None = None) -> int:
    """Record your manual actions into a replayable ``.ad`` script (interactive)."""
    args = ["record"] + (["-o", out] if out else []) + _platform_args(platform, device)
    return _run_interactive(args)


def replay(script: str) -> int:
    """Replay a recorded ``.ad`` script (streams progress; returns its exit code)."""
    return _run_interactive(["replay", script])


def passthrough(args: list[str]) -> int:
    """Escape hatch: run any agent-device subcommand with inherited stdio."""
    return _run_interactive(list(args))
