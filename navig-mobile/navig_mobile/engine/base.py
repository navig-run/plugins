"""Platform-agnostic device abstractions.

No third-party imports at module level — the concrete engines
(``engine.android``, ``engine.ios``) lazily import ``adbutils`` /
``pymobiledevice3`` inside their functions, so importing this module (and thus
``navig help``) never pulls a heavy dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable


class Platform(str, Enum):
    ANDROID = "android"
    IOS = "ios"


class Connection(str, Enum):
    USB = "usb"
    NETWORK = "network"
    RECOVERY = "recovery"
    DFU = "dfu"
    UNKNOWN = "unknown"


# progress callback: (done_bytes_or_items, total, note)
ProgressCb = Callable[[int, int, str], None]


@dataclass
class DeviceInfo:
    udid: str
    platform: Platform
    name: str = ""
    model: str = ""
    os_version: str = ""
    serial: str = ""
    manufacturer: str = ""
    product_type: str = ""
    battery: int | None = None  # percent 0-100
    storage_total: int | None = None  # bytes
    storage_free: int | None = None  # bytes
    developer_mode: bool | None = None
    rooted_or_jailbroken: bool | None = None
    connection: Connection = Connection.UNKNOWN
    trusted: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items()}
        d["platform"] = self.platform.value
        d["connection"] = self.connection.value
        return d


@dataclass
class AppInfo:
    app_id: str  # bundle id (iOS) / package name (Android)
    name: str = ""
    version: str = ""
    is_system: bool = False
    path: str = ""
    permissions: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@runtime_checkable
class Device(Protocol):
    """Everything a unified ``navig mobile`` verb needs. Both platform classes
    implement this, so commands dispatch on the protocol, not on ``isinstance``."""

    platform: Platform
    udid: str

    def info(self) -> DeviceInfo: ...

    def apps(self, *, system: bool = False) -> list[AppInfo]: ...

    def app_info(self, app_id: str) -> AppInfo | None: ...

    def install_app(self, path: str) -> None: ...

    def uninstall_app(self, app_id: str) -> None: ...

    def extract_app(self, app_id: str, dest: str) -> str: ...

    def ls(self, remote: str = "/") -> list[dict[str, Any]]: ...

    def pull(self, remote: str, local: str, progress: ProgressCb | None = None) -> str: ...

    def push(self, local: str, remote: str) -> None: ...

    def screenshot(self, dest: str) -> str: ...

    def backup(self, dest: str, *, encrypted: bool = False,
               progress: ProgressCb | None = None) -> str: ...

    def restore(self, src: str, *, password: str | None = None,
                progress: ProgressCb | None = None) -> None: ...

    def reboot(self, mode: str = "system") -> None: ...


# ── errors ───────────────────────────────────────────────────────────────────

class DeviceError(Exception):
    """Base for all navig-mobile device errors."""


class NoDeviceError(DeviceError):
    """No device connected/matched."""


class AmbiguousDeviceError(DeviceError):
    """More than one device is connected and no --udid was given."""

    def __init__(self, devices: list[DeviceInfo]):
        self.devices = devices
        listing = ", ".join(f"{d.udid} ({d.platform.value}/{d.name or d.model})"
                             for d in devices)
        super().__init__(
            f"{len(devices)} devices connected — pass --udid to pick one: {listing}"
        )


class PlatformUnavailableError(DeviceError):
    """The engine extra for a platform isn't installed."""

    def __init__(self, platform: Platform, hint: str):
        self.platform = platform
        self.hint = hint
        super().__init__(f"{platform.value} support not installed — {hint}")


# ── manager ──────────────────────────────────────────────────────────────────

class DeviceManager:
    """Discovers and resolves devices across both platforms.

    Each platform engine is imported lazily; if its extra isn't installed,
    discovery skips it (and ``resolve`` raises ``PlatformUnavailableError`` only
    when the user explicitly targets that platform).
    """

    # ── discovery ───────────────────────────────────────────────────────────
    def list_devices(self, platform: Platform | None = None) -> list[DeviceInfo]:
        out: list[DeviceInfo] = []
        if platform in (None, Platform.ANDROID):
            out.extend(self._safe_list(Platform.ANDROID))
        if platform in (None, Platform.IOS):
            out.extend(self._safe_list(Platform.IOS))
        return out

    def _safe_list(self, platform: Platform) -> list[DeviceInfo]:
        try:
            mod = self._engine_module(platform)
        except PlatformUnavailableError:
            return []
        try:
            return list(mod.list_devices())
        except Exception:
            # A backend hiccup (adb server down, usbmux missing) must not crash
            # discovery of the *other* platform.
            return []

    # ── resolution ──────────────────────────────────────────────────────────
    def resolve(self, udid: str | None = None,
                platform: Platform | None = None) -> Device:
        """Return the single addressed device.

        - explicit ``udid`` → that device (searched on the given/both platforms)
        - no udid, exactly one device connected → it
        - no udid, zero → ``NoDeviceError``; 2+ → ``AmbiguousDeviceError``
        """
        if udid:
            for p in ([platform] if platform else [Platform.ANDROID, Platform.IOS]):
                infos = {d.udid: d for d in self._safe_list(p)}
                if udid in infos:
                    return self._engine_module(p).get_device(udid)
            raise NoDeviceError(f"No connected device with udid {udid!r}.")

        devices = self.list_devices(platform)
        if not devices:
            raise NoDeviceError(
                "No device connected. Plug in a phone/tablet and enable USB "
                "debugging (Android) or tap Trust (iOS). See `navig mobile doctor`."
            )
        if len(devices) > 1:
            raise AmbiguousDeviceError(devices)
        d = devices[0]
        return self._engine_module(d.platform).get_device(d.udid)

    # ── engine loading ──────────────────────────────────────────────────────
    def _engine_module(self, platform: Platform):
        if platform is Platform.ANDROID:
            from navig_mobile.engine.android import device as mod
        else:
            from navig_mobile.engine.ios import device as mod
        mod.ensure_available()
        return mod
