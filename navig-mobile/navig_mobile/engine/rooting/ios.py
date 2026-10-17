"""iOS jailbreak assist — checkm8 eligibility + developer-mode / recovery drivers.

Detection is informational (a non-jailbreak tool can't reliably prove a device
is jailbroken); we report chip-based checkm8 eligibility and developer-mode
state. Recovery / developer-mode are driven via the ``pymobiledevice3`` CLI.
"""

from __future__ import annotations

import re
from typing import Any

# checkm8 (the checkra1n/palera1n bootrom exploit) covers A5–A11. On iPhone that
# is iPhoneN,x with N ≤ 10 (iPhone 8/8+/X). A12+ (iPhone XS = iPhone11,x) is not
# vulnerable. iPad up to the A10 generation (iPad7,x) is broadly eligible.
_IPHONE_CHECKM8_MAX = 10
_IPAD_CHECKM8_MAX = 7


def checkm8_eligible(product_type: str) -> tuple[bool | None, str]:
    """(eligible, human reason) for the checkm8 bootrom exploit."""
    if not product_type:
        return None, "Unknown device — cannot assess eligibility."
    m = re.match(r"iPhone(\d+),", product_type)
    if m:
        gen = int(m.group(1))
        if gen <= _IPHONE_CHECKM8_MAX:
            return True, (f"{product_type}: A11 or older — checkm8-vulnerable "
                          "(checkra1n / palera1n).")
        return False, (f"{product_type}: A12 or newer — NOT checkm8-vulnerable; "
                       "no public untethered jailbreak on current iOS.")
    m = re.match(r"iPad(\d+),", product_type)
    if m:
        gen = int(m.group(1))
        if gen <= _IPAD_CHECKM8_MAX:
            return True, f"{product_type}: likely checkm8-eligible (A10 or older)."
        return False, f"{product_type}: likely NOT checkm8-eligible (A12+)."
    return None, f"{product_type}: eligibility unknown for this model."


def _pmd3(args: list[str], udid: str, *, parse_json: bool = False, timeout: int = 60) -> Any:
    """Run a pymobiledevice3 command for a device. Isolated for test monkeypatching."""
    from navig_mobile.engine.ios import device as iosdev

    return iosdev._run([*args, "--udid", udid], parse_json=parse_json, timeout=timeout)


def developer_mode_status(udid: str) -> bool | None:
    try:
        out = str(_pmd3(["amfi", "developer-mode-status"], udid, timeout=30)).strip().lower()
    except Exception:
        return None
    if "true" in out or "enabled" in out:
        return True
    if "false" in out or "disabled" in out:
        return False
    return None


def enable_developer_mode(udid: str) -> str:
    """Request iOS Developer Mode (device reboots; user confirms the on-device prompt)."""
    return str(_pmd3(["amfi", "enable-developer-mode"], udid, timeout=60))


def enter_recovery(udid: str) -> str:
    return str(_pmd3(["restore", "enter"], udid, timeout=60))


def exit_recovery(udid: str) -> str:
    return str(_pmd3(["restore", "exit"], udid, timeout=60))


def jailbreak_status(device: Any) -> dict[str, Any]:
    """Informational jailbreak/eligibility status for an iOS device."""
    info = device.info()
    product_type = info.product_type or info.model
    eligible, reason = checkm8_eligible(product_type)
    dev_mode = developer_mode_status(device.udid)
    if eligible is True:
        tool = "palera1n (rootless, iOS 15+) or checkra1n (older iOS)"
    elif eligible is False:
        tool = "No public checkm8 jailbreak for this chip."
    else:
        tool = "Unknown."
    return {
        "platform": "ios",
        "product_type": product_type,
        "os_version": info.os_version,
        "checkm8_eligible": eligible,
        "eligibility": reason,
        "developer_mode": dev_mode,
        "recommended_tool": tool,
    }
