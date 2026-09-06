"""Mobile toolchain detection — powers ``navig mobile doctor``.

Mirrors the navig-antivirus ``system_scan.detect()`` pattern: probe each tool
(Python package via ``importlib.util.find_spec`` — no import, so ``navig help``
stays fast; external binary via ``shutil.which`` + known install dirs), and
return a dataclass with a human ``note`` / ``install_hint`` for what's missing.

No third-party imports happen here — detection is stdlib only.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from navig.core.proc_text import decode_console_result

_WIN = sys.platform.startswith("win")
_MAC = sys.platform == "darwin"


@dataclass
class ToolStatus:
    key: str
    label: str
    kind: str  # "python" | "binary" | "driver"
    found: bool
    detail: str = ""  # path or version
    note: str = ""
    install_hint: str = ""


@dataclass
class MobileToolchain:
    tools: list[ToolStatus] = field(default_factory=list)

    def get(self, key: str) -> ToolStatus | None:
        return next((t for t in self.tools if t.key == key), None)

    def _ok(self, key: str) -> bool:
        t = self.get(key)
        return bool(t and t.found)

    @property
    def android_ready(self) -> bool:
        return self._ok("adbutils")

    @property
    def ios_ready(self) -> bool:
        return self._ok("pymobiledevice3")

    @property
    def uiauto_ready(self) -> bool:
        """App UI automation (agent-device) available for driving/verifying apps."""
        return self._ok("agent-device")


# ── low-level probes ─────────────────────────────────────────────────────────

def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _module_version(name: str) -> str:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:
        return ""


def _which(names: list[str], extra_dirs: list[Path] | None = None) -> Path | None:
    for n in names:
        hit = shutil.which(n)
        if hit:
            return Path(hit)
    for d in extra_dirs or []:
        if not d:
            continue
        for n in names:
            for cand in (d / n, d / f"{n}.exe"):
                if cand.exists():
                    return cand
    return None


def _bin_version(exe: Path, args: list[str] | None = None) -> str:
    try:
        out = decode_console_result(subprocess.run(
            [str(exe), *(args or ["--version"])],
            capture_output=True, timeout=4,
        ))
        line = (out.stdout or out.stderr or "").strip().splitlines()
        return line[0].strip() if line else ""
    except Exception:
        return ""


def _android_sdk_dirs() -> list[Path]:
    dirs: list[Path] = []
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        v = os.environ.get(env)
        if v:
            dirs.append(Path(v) / "platform-tools")
    home = Path.home()
    dirs += [
        home / "Android" / "Sdk" / "platform-tools",
        home / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools",
        home / "Library" / "Android" / "sdk" / "platform-tools",
        Path("/usr/lib/android-sdk/platform-tools"),
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
    ]
    return dirs


# ── driver probe: Apple usbmux (needed for iOS-over-USB) ─────────────────────

def _detect_usbmux() -> ToolStatus:
    key, label, kind = "usbmux", "Apple usbmux (iOS USB)", "driver"
    if _WIN:
        candidates = [
            Path(os.environ.get("CommonProgramFiles", r"C:\Program Files\Common Files"))
            / "Apple" / "Mobile Device Support",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "Common Files" / "Apple" / "Mobile Device Support",
        ]
        # "Apple Devices" (Store app) or iTunes both install AMDS.
        found = any(p.exists() for p in candidates)
        return ToolStatus(
            key, label, kind, found,
            detail="Apple Mobile Device Support present" if found else "",
            note="" if found else "Required for iOS over USB on Windows.",
            install_hint="" if found else "Install the 'Apple Devices' app (Microsoft Store) or iTunes.",
        )
    if _MAC:
        return ToolStatus(key, label, kind, True, detail="built into macOS")
    # Linux — usbmuxd daemon / socket
    sock = Path("/var/run/usbmuxd")
    found = sock.exists() or shutil.which("usbmuxd") is not None
    return ToolStatus(
        key, label, kind, found,
        detail="usbmuxd running" if found else "",
        note="" if found else "Required for iOS over USB.",
        install_hint="" if found else "Install usbmuxd (e.g. `apt install usbmuxd`).",
    )


# ── public API ───────────────────────────────────────────────────────────────

def detect() -> MobileToolchain:
    """Detect the full mobile toolchain. Stdlib-only; safe to call anytime."""
    tools: list[ToolStatus] = []
    sdk = _android_sdk_dirs()

    # Python engines (extras) ------------------------------------------------
    adbutils = _has_module("adbutils")
    tools.append(ToolStatus(
        "adbutils", "adbutils (Android engine)", "python", adbutils,
        detail=_module_version("adbutils"),
        note="" if adbutils else "Android device engine.",
        install_hint="" if adbutils else 'pip install "navig-mobile[android]"',
    ))
    pmd3 = _has_module("pymobiledevice3")
    tools.append(ToolStatus(
        "pymobiledevice3", "pymobiledevice3 (iOS engine)", "python", pmd3,
        detail=_module_version("pymobiledevice3"),
        note="" if pmd3 else "iOS device engine (the '3uTools' core).",
        install_hint="" if pmd3 else 'pip install "navig-mobile[ios]"',
    ))

    # Android binaries -------------------------------------------------------
    adb = _which(["adb"], sdk)
    tools.append(ToolStatus(
        "adb", "adb (Android platform-tools)", "binary", adb is not None,
        detail=(_bin_version(adb, ["--version"]) if adb else ""),
        note="" if adb else "Needed for fastboot/sideload/bugreport/mirror.",
        install_hint="" if adb else _adb_hint(),
    ))
    fastboot = _which(["fastboot"], sdk)
    tools.append(ToolStatus(
        "fastboot", "fastboot (bootloader flashing)", "binary", fastboot is not None,
        detail=(_bin_version(fastboot, ["--version"]) if fastboot else ""),
        note="" if fastboot else "Needed for unlock/flash/root.",
        install_hint="" if fastboot else _adb_hint(),
    ))
    scrcpy = _which(["scrcpy"], sdk)
    tools.append(ToolStatus(
        "scrcpy", "scrcpy (screen mirror)", "binary", scrcpy is not None,
        detail=(_bin_version(scrcpy, ["--version"]) if scrcpy else ""),
        note="" if scrcpy else "Screen mirroring / control (Android).",
        install_hint="" if scrcpy else _scrcpy_hint(),
    ))

    # iOS driver -------------------------------------------------------------
    tools.append(_detect_usbmux())

    # App UI automation / verification (agent-device — Node CLI, detected) ----
    node = _which(["node"])
    tools.append(ToolStatus(
        "node", "Node.js (agent-device runtime)", "binary", node is not None,
        detail=(_bin_version(node, ["--version"]) if node else ""),
        note="" if node else "Runtime for agent-device (app UI automation).",
        install_hint="" if node else _node_hint(),
    ))
    agent_device = _which(["agent-device"])
    tools.append(ToolStatus(
        "agent-device", "agent-device (app UI automation)", "binary",
        agent_device is not None,
        detail=(_bin_version(agent_device, ["--version"]) if agent_device else ""),
        note="" if agent_device else "Drive/verify app UIs — a11y snapshots, tap/fill, "
        "evidence capture, replay (iOS/Android/web/desktop; MIT).",
        install_hint="" if agent_device else "npm install -g agent-device@latest  "
        "(needs Node 22+; then `agent-device doctor`)",
    ))

    # Forensics / spyware / instrumentation (detected, not bundled) ----------
    mvt_bin = _which(["mvt-ios", "mvt-android"])
    mvt = mvt_bin is not None or _has_module("mvt")
    tools.append(ToolStatus(
        "mvt", "MVT (spyware scan)", "python", mvt,
        detail=(_module_version("mvt") or (str(mvt_bin) if mvt_bin else "")),
        note="" if mvt else "Amnesty Mobile Verification Toolkit — Pegasus/mercenary spyware IOC scan.",
        install_hint="" if mvt else "pip install mvt  (use-restricted license — review before use)",
    ))
    frida = _which(["frida"]) is not None or _has_module("frida")
    tools.append(ToolStatus(
        "frida", "frida (instrumentation)", "python", frida,
        detail=_module_version("frida"),
        note="" if frida else "Dynamic instrumentation (dev-tools; needs on-device frida-server).",
        install_hint="" if frida else "pip install frida-tools",
    ))
    objection = _which(["objection"]) is not None or _has_module("objection")
    tools.append(ToolStatus(
        "objection", "objection (runtime mobile toolkit)", "python", objection,
        detail=_module_version("objection"),
        note="" if objection else "Runtime mobile exploration (GPLv3 — invoked as a subprocess).",
        install_hint="" if objection else "pip install objection",
    ))
    ileapp = _which(["ileapp", "ileappGUI"]) is not None or _has_module("ileapp")
    aleapp = _which(["aleapp", "aleappGUI"]) is not None or _has_module("aleapp")
    tools.append(ToolStatus(
        "ileapp", "iLEAPP (iOS artifact parser)", "python", ileapp,
        detail=_module_version("ileapp"),
        note="" if ileapp else "iOS logs/events/plist forensic parser → HTML/timeline.",
        install_hint="" if ileapp else "pip install ileapp",
    ))
    tools.append(ToolStatus(
        "aleapp", "ALEAPP (Android artifact parser)", "python", aleapp,
        detail=_module_version("aleapp"),
        note="" if aleapp else "Android logs/events/protobuf forensic parser → HTML/timeline.",
        install_hint="" if aleapp else "pip install aleapp",
    ))

    return MobileToolchain(tools=tools)


def _adb_hint() -> str:
    if _WIN:
        return "Install Android platform-tools (winget install Google.PlatformTools) or set ANDROID_HOME."
    if _MAC:
        return "brew install android-platform-tools"
    return "Install android-sdk-platform-tools (e.g. `apt install adb fastboot`)."


def _scrcpy_hint() -> str:
    if _WIN:
        return "winget install Genymobile.scrcpy"
    if _MAC:
        return "brew install scrcpy"
    return "apt install scrcpy  (or snap install scrcpy)"


def _node_hint() -> str:
    if _WIN:
        return "winget install OpenJS.NodeJS  (Node 22+ for agent-device)"
    if _MAC:
        return "brew install node  (Node 22+ for agent-device)"
    return "Install Node 22+ (e.g. via nodejs.org or your package manager) for agent-device."
