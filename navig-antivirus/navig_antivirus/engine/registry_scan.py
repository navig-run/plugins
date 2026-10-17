"""Chrome extension-registry force-install audit (Windows / winreg).

Apps and bundleware can force-install Chrome extensions by writing keys under
``…\\Software\\Google\\Chrome\\Extensions\\<id>`` (per-profile-agnostic) or the
``ExtensionInstallForcelist`` policy. This flags PUP/bundleware signatures:
an affiliate ``install_parameter`` (``clid=…``), an insecure ``http://`` update
URL, or an unknown non-Web-Store source. Read-only.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

_WIN = sys.platform.startswith("win")

_FORCE_ROOTS = [
    (r"HKCU", r"SOFTWARE\Google\Chrome\Extensions"),
    (r"HKLM", r"SOFTWARE\Google\Chrome\Extensions"),
    (r"HKLM", r"SOFTWARE\Wow6432Node\Google\Chrome\Extensions"),
]
_POLICY_ROOTS = [
    (r"HKLM", r"SOFTWARE\Policies\Google\Chrome"),
    (r"HKCU", r"SOFTWARE\Policies\Google\Chrome"),
]


@dataclass
class RegExt:
    hive: str
    ext_id: str
    update_url: str = ""
    install_parameter: str = ""
    path: str = ""
    flags: list[str] = field(default_factory=list)


@dataclass
class RegReport:
    available: bool
    force_installed: list[RegExt] = field(default_factory=list)
    forcelist: list[str] = field(default_factory=list)   # ExtensionInstallForcelist policy
    blocklist: list[str] = field(default_factory=list)   # ExtensionInstallBlocklist policy
    # Registry locations that EXIST-or-might but could NOT be read (permission denied,
    # etc.) — as opposed to genuinely-absent keys. A denied HKLM root is where enterprise
    # and malware force-installs live, so treating "couldn't read" as "nothing there"
    # would render a false "clean" verdict (the scan_extensions false-clean class). The
    # caller surfaces these so an incomplete audit is never mistaken for a clean one.
    unreadable: list[str] = field(default_factory=list)


def _hkey(name: str):
    import winreg
    return {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}[name]


def _note_open_failure(exc: OSError, unreadable: list[str] | None, label: str) -> None:
    """A missing key (``FileNotFoundError``) is a genuinely-clean location — no signal.
    Any other ``OSError`` (permission denied, etc.) means we could NOT look, which is a
    coverage gap, not evidence of absence — record it so the caller can't report clean."""
    if isinstance(exc, FileNotFoundError):
        return
    if unreadable is not None:
        unreadable.append(label or "<registry>")


def _read_values(
    root, subkey: str, *, unreadable: list[str] | None = None, label: str = ""
) -> dict:
    import winreg
    out = {}
    try:
        with winreg.OpenKey(root, subkey) as k:
            i = 0
            while True:
                try:
                    name, val, _ = winreg.EnumValue(k, i)
                    out[name] = val
                    i += 1
                except OSError:
                    break  # end of enumeration (ERROR_NO_MORE_ITEMS) — normal
    except OSError as exc:
        _note_open_failure(exc, unreadable, label)
    return out


def _subkeys(
    root, subkey: str, *, unreadable: list[str] | None = None, label: str = ""
) -> list[str]:
    import winreg
    names = []
    try:
        with winreg.OpenKey(root, subkey) as k:
            i = 0
            while True:
                try:
                    names.append(winreg.EnumKey(k, i))
                    i += 1
                except OSError:
                    break  # end of enumeration (ERROR_NO_MORE_ITEMS) — normal
    except OSError as exc:
        _note_open_failure(exc, unreadable, label)
    return names


def _policy_list(
    root, base: str, name: str, *, unreadable: list[str] | None = None, label: str = ""
) -> list[str]:
    vals = _read_values(root, base + "\\" + name, unreadable=unreadable, label=label)
    return [str(v) for _, v in sorted(vals.items())]


def scan_registry() -> RegReport:
    """Enumerate registry-forced Chrome extensions + install policies, flagging PUP signatures."""
    if not _WIN:
        return RegReport(available=False)
    rep = RegReport(available=True)

    for hive, base in _FORCE_ROOTS:
        root = _hkey(hive)
        for ext_id in _subkeys(root, base, unreadable=rep.unreadable, label=f"{hive}\\{base}"):
            vals = _read_values(
                root, base + "\\" + ext_id,
                unreadable=rep.unreadable, label=f"{hive}\\{base}\\{ext_id}",
            )
            rx = RegExt(
                hive=hive, ext_id=ext_id,
                update_url=str(vals.get("update_url", "")),
                install_parameter=str(vals.get("install_parameter", "")),
                path=str(vals.get("path", "")),
            )
            if "clid=" in rx.install_parameter or (rx.install_parameter and "&" in rx.install_parameter):
                rx.flags.append("affiliate install_parameter (bundleware/PUP)")
            if rx.update_url.startswith("http://"):
                rx.flags.append("insecure http:// update_url")
            rep.force_installed.append(rx)

    for hive, base in _POLICY_ROOTS:
        root = _hkey(hive)
        rep.forcelist += _policy_list(
            root, base, "ExtensionInstallForcelist",
            unreadable=rep.unreadable, label=f"{hive}\\{base}\\ExtensionInstallForcelist",
        )
        rep.blocklist += _policy_list(
            root, base, "ExtensionInstallBlocklist",
            unreadable=rep.unreadable, label=f"{hive}\\{base}\\ExtensionInstallBlocklist",
        )
    return rep
