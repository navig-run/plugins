"""Meta Quest (Horizon OS) operations over ADB — the engine behind ``navig mobile quest``.

A Quest is an Android device, so everything here rides plain ``adb``; what is
Quest-specific is *which* knobs exist and how to verify them:

* **Render tweaks** are ``debug.oculus.*`` system properties. They are volatile —
  a reboot clears them — and a value reading back from ``getprop`` only proves it
  was *stored*. Whether a game honours it is a separate question, answered by the
  compositor's own once-a-second ``VrApi`` log line (``FPS=120/120,…,Fov=2D,…``),
  which :func:`parse_vrapi` reads. Store games have been observed honouring them
  on Horizon OS build 207 (Quest 3, Android 14).
* **Wireless ADB** has no on-device toggle on current Quest builds (no "pair with
  code" screen); it is enabled from a USB session with ``adb tcpip 5555`` and is
  lost on reboot.
* **Proximity override**: the headset sleeps when nobody wears it, so a mirror or
  recording yields black/empty output. ``prox_close`` fakes "worn";
  ``automation_disable`` hands control back to the real sensor.

All device I/O goes through an injectable ``run(args) -> (rc, out)`` so the logic
is unit-testable without hardware.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass, field
from typing import Callable

Runner = Callable[[list[str]], "tuple[int, str]"]

QUEST_MODELS = ("quest", "eureka", "hollywood", "seacliff", "panther", "monterey")
"""``ro.product.model``/``device`` substrings that identify Meta headsets
(Quest 3 = eureka, Quest 2 = hollywood, Pro = seacliff, 3S = panther)."""

PROP_PREFIX = "debug.oculus."


@dataclass
class Tweaks:
    """One set of ``debug.oculus.*`` render/perf knobs. ``None`` = leave unset."""

    texture_width: int | None = None
    texture_height: int | None = None
    refresh_rate: int | None = None
    foveation_level: int | None = None
    foveation_dynamic: int | None = None
    cpu_level: int | None = None
    gpu_level: int | None = None

    _PROPS = {
        "texture_width": "textureWidth",
        "texture_height": "textureHeight",
        "refresh_rate": "refreshRate",
        "foveation_level": "foveation.level",
        "foveation_dynamic": "foveation.dynamic",
        "cpu_level": "cpuLevel",
        "gpu_level": "gpuLevel",
    }

    def props(self) -> dict[str, str]:
        """Only the knobs that are set, as ``{full prop name: value}``."""
        out: dict[str, str] = {}
        for attr, suffix in self._PROPS.items():
            val = getattr(self, attr)
            if val is not None:
                out[PROP_PREFIX + suffix] = str(val)
        return out

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, d: dict | None) -> "Tweaks":
        d = d or {}
        return cls(**{k: d[k] for k in cls._PROPS if k in d})

    @classmethod
    def all_props(cls) -> list[str]:
        return [PROP_PREFIX + s for s in cls._PROPS.values()]


# Named profiles. "quality" is the set verified live (120 fps, Fov 2D, CPU/GPU 4/4,
# ~37 °C in Beat Saber on a Quest 3, 2026-09-30). "balanced" trades sharpness for
# heat/battery headroom in heavy titles.
PROFILES: dict[str, Tweaks] = {
    "quality": Tweaks(3072, 3216, 120, 2, 1, 4, 4),
    "balanced": Tweaks(2560, 2688, 90, 2, 1, 3, 3),
}


# ── adb plumbing ────────────────────────────────────────────────────────────

def make_runner(adb: str, serial: str | None = None, timeout: int = 60) -> Runner:
    """A ``run(args)`` bound to one adb binary (+ optional ``-s serial``)."""

    def run(args: list[str]) -> tuple[int, str]:
        argv = [adb] + (["-s", serial] if serial else []) + list(args)
        try:
            p = subprocess.run(argv, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "timed out"
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()

    return run


class QuestOffline(RuntimeError):
    """The adb transport is gone (headset asleep, rebooted, or off Wi-Fi)."""


_OFFLINE = ("device offline", "error: closed", "no devices/emulators", "not found",
            "unauthorized", "timed out")


def shell(run: Runner, cmd: str) -> str:
    """Run a device shell command. Raises :class:`QuestOffline` instead of returning
    adb's transport error as if it were the command's output."""
    rc, out = run(["shell", cmd])
    if rc != 0 and any(s in out.lower() for s in _OFFLINE):
        raise QuestOffline(out.splitlines()[-1] if out else "device offline")
    return out


def is_quest(run: Runner) -> bool:
    ident = (shell(run, "getprop ro.product.model") + " "
             + shell(run, "getprop ro.product.device")).lower()
    return any(m in ident for m in QUEST_MODELS)


# ── adb device discovery ────────────────────────────────────────────────────

@dataclass
class AdbDevice:
    serial: str
    state: str
    model: str = ""

    @property
    def is_wifi(self) -> bool:
        return ":" in self.serial

    @property
    def looks_quest(self) -> bool:
        return any(m in self.model.lower() for m in QUEST_MODELS)


def parse_adb_devices(text: str) -> list[AdbDevice]:
    """Parse ``adb devices -l`` (``unauthorized``/``offline`` rows included)."""
    devs: list[AdbDevice] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("List of devices", "*")):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        model = ""
        for tok in parts[2:]:
            if tok.startswith(("model:", "device:")):
                model += tok.split(":", 1)[1] + " "
        devs.append(AdbDevice(parts[0], parts[1], model.strip()))
    return devs


def find_quests(adb_run: Runner) -> list[AdbDevice]:
    """Connected Quest headsets (any state), Wi-Fi rows first."""
    devs = [d for d in parse_adb_devices(adb_run(["devices", "-l"])[1]) if d.looks_quest]
    return sorted(devs, key=lambda d: (not d.is_wifi, d.serial))


def wifi_ip(run: Runner) -> str:
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/", shell(run, "ip -4 addr show wlan0"))
    return m.group(1) if m else ""


# ── status ──────────────────────────────────────────────────────────────────

@dataclass
class QuestStatus:
    model: str = ""
    android: str = ""
    horizon_build: str = ""
    security_patch: str = ""
    battery: int | None = None
    awake: bool | None = None
    wifi_ip: str = ""
    storage: str = ""
    props: dict[str, str] = field(default_factory=dict)


def read_props(run: Runner) -> dict[str, str]:
    """Current ``debug.oculus.*`` values (empty string = unset)."""
    return {p: shell(run, f"getprop {p}").strip() for p in Tweaks.all_props()}


def status(run: Runner) -> QuestStatus:
    st = QuestStatus(
        model=shell(run, "getprop ro.product.model").strip(),
        android=shell(run, "getprop ro.build.version.release").strip(),
        horizon_build=shell(run, "getprop ro.vros.build.version").strip(),
        security_patch=shell(run, "getprop ro.build.version.security_patch").strip(),
        wifi_ip=wifi_ip(run),
    )
    m = re.search(r"level:\s*(\d+)", shell(run, "dumpsys battery"))
    st.battery = int(m.group(1)) if m else None
    m = re.search(r"mWakefulness=(\w+)", shell(run, "dumpsys power"))
    st.awake = (m.group(1) == "Awake") if m else None
    df = shell(run, "df -h /data").splitlines()
    if len(df) >= 2:
        cols = df[-1].split()
        if len(cols) >= 4:
            st.storage = f"{cols[3]} free of {cols[1]}"
    st.props = read_props(run)
    return st


# ── tweaks ──────────────────────────────────────────────────────────────────

@dataclass
class PropResult:
    prop: str
    wanted: str
    readback: str

    @property
    def ok(self) -> bool:
        return self.readback == self.wanted


def apply_tweaks(run: Runner, tweaks: Tweaks) -> list[PropResult]:
    """``setprop`` each knob, then read it back — a write is only reported OK if
    ``getprop`` returns exactly what was set."""
    results = []
    for prop, val in tweaks.props().items():
        shell(run, f"setprop {prop} {val}")
        results.append(PropResult(prop, val, shell(run, f"getprop {prop}").strip()))
    return results


def reset_tweaks(run: Runner) -> list[PropResult]:
    """Clear every knob back to the headset's own defaults (empty value)."""
    results = []
    for prop in Tweaks.all_props():
        shell(run, f"setprop {prop} ''")
        results.append(PropResult(prop, "", shell(run, f"getprop {prop}").strip()))
    return results


def tweaks_missing(run: Runner, tweaks: Tweaks) -> bool:
    """True when the headset no longer carries these tweaks (e.g. after a reboot)."""
    current = read_props(run)
    return any(current.get(p, "") != v for p, v in tweaks.props().items())


# ── live performance (proof the tweaks reach the game) ─────────────────────

@dataclass
class VrApiSample:
    time: str
    fps: str
    target_fps: str
    foveation: str
    cpu_gpu: str
    temp: str


_VRAPI = re.compile(r"^(?P<time>\S+ \S+).*?FPS=(?P<fps>\d+)/(?P<target>\d+)")


def parse_vrapi(text: str) -> list[VrApiSample]:
    """Parse the compositor's ``VrApi`` lines (``FPS=120/120,…,Fov=2D,CPU4/GPU=4/4,…``)."""
    out = []
    for line in text.splitlines():
        m = _VRAPI.search(line)
        if not m:
            continue
        fields = dict(f.split("=", 1) for f in line.split(",") if "=" in f)
        out.append(VrApiSample(
            time=m.group("time"),
            fps=m.group("fps"),
            target_fps=m.group("target"),
            foveation=fields.get("Fov", ""),
            cpu_gpu=next((v for k, v in fields.items() if k.startswith("CPU")), ""),
            temp=(fields.get("Temp", "").split("/")[0]),
        ))
    return out


def perf(run: Runner, last: int = 5) -> list[VrApiSample]:
    """The latest VrApi samples (only produced while a VR app is running)."""
    return parse_vrapi(shell(run, "logcat -d -s VrApi"))[-last:]


# ── proximity (keep awake without wearing it) ──────────────────────────────

def set_awake_override(run: Runner, on: bool) -> str:
    action = "prox_close" if on else "automation_disable"
    out = shell(run, f"am broadcast -a com.oculus.vrpowermanager.{action}")
    if on:
        shell(run, "input keyevent KEYCODE_WAKEUP")
    return out


# ── mirror / record ─────────────────────────────────────────────────────────

QUEST3_EYE_CROP = "2064:2208:0:0"
"""Left eye of the Quest 3's 4128×2208 side-by-side frame — one clean view."""


def scrcpy_args(serial: str, *, both_eyes: bool = False, record: str | None = None,
                max_size: int = 1920, bit_rate: str = "20M",
                window: bool = True, time_limit: int | None = None) -> list[str]:
    args = ["-s", serial, f"--max-size={max_size}", f"--video-bit-rate={bit_rate}",
            "--window-title=Quest"]
    if not both_eyes:
        args.append(f"--crop={QUEST3_EYE_CROP}")
    if record:
        args.append(f"--record={record}")
    if not window:
        args.append("--no-window")
    if time_limit:
        args.append(f"--time-limit={time_limit}")
    return args
