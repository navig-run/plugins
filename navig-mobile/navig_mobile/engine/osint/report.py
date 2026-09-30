"""Device-anchored OSINT: identity gathering + installed-app privacy report.

Android identity/permissions come from getprop / settings / dumpsys over the adb
shell we already hold. iOS identity comes from the full lockdown value set. The
app privacy report flags high-risk permissions each app declares; deeper analysis
is handed off to the war-room `osint-analyst` / `forensics-investigator` agents.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from navig_mobile.engine.base import DeviceError, Platform

# High-risk Android runtime permissions → plain-words label.
DANGEROUS_PERMS: dict[str, str] = {
    "android.permission.CAMERA": "camera",
    "android.permission.RECORD_AUDIO": "microphone",
    "android.permission.ACCESS_FINE_LOCATION": "precise location",
    "android.permission.ACCESS_COARSE_LOCATION": "approximate location",
    "android.permission.ACCESS_BACKGROUND_LOCATION": "background location",
    "android.permission.READ_CONTACTS": "read contacts",
    "android.permission.WRITE_CONTACTS": "modify contacts",
    "android.permission.READ_SMS": "read SMS",
    "android.permission.SEND_SMS": "send SMS",
    "android.permission.READ_CALL_LOG": "read call log",
    "android.permission.READ_PHONE_STATE": "phone identity/state",
    "android.permission.READ_PHONE_NUMBERS": "phone number",
    "android.permission.READ_CALENDAR": "read calendar",
    "android.permission.BODY_SENSORS": "body sensors",
    "android.permission.ACTIVITY_RECOGNITION": "physical activity",
    "android.permission.READ_EXTERNAL_STORAGE": "read storage",
    "android.permission.MANAGE_EXTERNAL_STORAGE": "all-files storage",
    "android.permission.SYSTEM_ALERT_WINDOW": "draw over other apps",
    "android.permission.REQUEST_INSTALL_PACKAGES": "install apps",
    "android.permission.QUERY_ALL_PACKAGES": "list all installed apps",
    "android.permission.BIND_ACCESSIBILITY_SERVICE": "accessibility (screen) access",
    "android.permission.PACKAGE_USAGE_STATS": "app-usage stats",
}

_PERM_RE = re.compile(r"[a-zA-Z][\w.]*\.permission\.[A-Z0-9_]+")


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ── identity ─────────────────────────────────────────────────────────────────

def device_identity(device: Any) -> dict[str, Any]:
    if device.platform is Platform.ANDROID:
        return _android_identity(device)
    return _ios_identity(device)


def _android_identity(device: Any) -> dict[str, Any]:
    raw = getattr(device, "_raw", None)
    if raw is None:
        raise DeviceError("osint device is Android-only here (no adb handle).")

    def prop(n: str) -> str:
        try:
            return (raw.getprop(n) or "").strip()
        except Exception:
            return ""

    def shell(cmd: str) -> str:
        try:
            return (raw.shell(cmd) or "").strip()
        except Exception:
            return ""

    android_id = shell("settings get secure android_id")
    return {
        "platform": "android",
        "model": prop("ro.product.model"),
        "manufacturer": prop("ro.product.manufacturer"),
        "device": prop("ro.product.device"),
        "serial": prop("ro.serialno"),
        "android_id": android_id if android_id and "null" not in android_id else None,
        "os_version": prop("ro.build.version.release"),
        "build_fingerprint": prop("ro.build.fingerprint"),
        "security_patch": prop("ro.build.version.security_patch"),
        "baseband": prop("gsm.version.baseband") or None,
        # IMEI is permission-gated on modern Android — surfaced only if reachable.
        "note": "IMEI/MEID are permission-gated on modern Android; use a rooted read or "
                "`forensics acquire` for regulated identifiers.",
    }


def _ios_identity(device: Any) -> dict[str, Any]:
    getter = getattr(device, "lockdown_values", None)
    vals = getter() if callable(getter) else {}
    keys = [
        "DeviceName", "ProductType", "ProductVersion", "BuildVersion", "ModelNumber",
        "RegionInfo", "SerialNumber", "UniqueDeviceID", "UniqueChipID",
        "InternationalMobileEquipmentIdentity", "InternationalMobileEquipmentIdentity2",
        "MobileEquipmentIdentifier", "IntegratedCircuitCardIdentity",
        "WiFiAddress", "BluetoothAddress", "EthernetAddress", "PhoneNumber",
    ]
    out: dict[str, Any] = {"platform": "ios"}
    for k in keys:
        if vals.get(k):
            out[k] = vals[k]
    return out


# ── app privacy ──────────────────────────────────────────────────────────────

def app_permissions(device: Any, package: str) -> dict[str, Any]:
    """Dangerous-permission profile for a single Android app (via dumpsys)."""
    raw = getattr(device, "_raw", None)
    if raw is None:
        return {"package": package, "platform": "ios",
                "note": "iOS app permissions need the IPA/entitlements — not available over lockdown."}
    try:
        out = raw.shell(f"dumpsys package {package}") or ""
    except Exception as exc:
        raise DeviceError(f"dumpsys failed for {package}: {exc}") from exc
    found = set(_PERM_RE.findall(out))
    dangerous = {p: DANGEROUS_PERMS[p] for p in found if p in DANGEROUS_PERMS}
    return {
        "package": package,
        "platform": "android",
        "risk_score": len(dangerous),
        "dangerous": dangerous,
        "permission_count": len(found),
    }


def app_privacy_scan(device: Any, *, limit: int = 40) -> dict[str, Any]:
    """Scan up to ``limit`` user apps and rank them by high-risk permissions."""
    if device.platform is not Platform.ANDROID:
        return {"platform": "ios", "scanned": 0, "apps": [],
                "note": "App privacy scanning is Android-only (dumpsys). iOS entitlements "
                        "need the IPA."}
    apps = device.apps(system=False)
    total = len(apps)
    scanned = apps[:limit]
    results = []
    for a in scanned:
        prof = app_permissions(device, a.app_id)
        if prof.get("risk_score"):
            results.append({"app": a.app_id, "name": a.name,
                            "risk_score": prof["risk_score"],
                            "grants": sorted(prof["dangerous"].values())})
    results.sort(key=lambda r: -r["risk_score"])
    return {
        "platform": "android",
        "total_user_apps": total,
        "scanned": len(scanned),
        "flagged": len(results),
        "truncated": total > limit,
        "apps": results,
    }


# ── combined report ──────────────────────────────────────────────────────────

def build_report(device: Any, *, limit: int = 40) -> dict[str, Any]:
    return {
        "generated_at": _utcnow(),
        "udid": device.udid,
        "identity": device_identity(device),
        "app_privacy": app_privacy_scan(device, limit=limit),
        "handoff": "For deeper analysis route this to the war-room agents "
                   "(#osint / #forensics): navig-run/community war-room-space.",
    }


def write_report(report: dict[str, Any], out: str | None = None) -> str:
    from navig_mobile import config

    dest = Path(out) if out else (config.mobile_dir() / "osint" /
                                  f"osint-{report.get('udid', 'device')}.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return str(dest)
