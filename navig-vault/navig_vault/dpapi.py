"""Windows DPAPI (``CryptProtectData``) through ctypes — no pywin32 dependency.

User scope: a blob protected here unprotects only for the same Windows user
account on the same machine (and survives a password change made by that user).
It is the vault's ``dpapi`` keyslot protector on Windows.

``available()`` is False everywhere else; callers must check it first.
"""

from __future__ import annotations

import sys

__all__ = ["DpapiError", "available", "protect", "unprotect"]

# Not CRYPTPROTECT_LOCAL_MACHINE: user scope is the point (any local user could
# unprotect a machine-scope blob). UI_FORBIDDEN: never pop a dialog from a daemon.
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_ENTROPY = b"navig-vault/keyslot/v1"


class DpapiError(RuntimeError):
    """DPAPI refused to protect or unprotect (wrong user, corrupt blob, no DPAPI)."""


def available() -> bool:
    return sys.platform == "win32"


def _call(fn_name: str, data: bytes) -> bytes:
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _blob(raw: bytes) -> tuple[_Blob, ctypes.Array]:
        buf = ctypes.create_string_buffer(raw, len(raw))
        return _Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    fn = getattr(crypt32, fn_name)
    fn.restype = wintypes.BOOL

    data_in, _keep_in = _blob(data)
    entropy, _keep_e = _blob(_ENTROPY)
    data_out = _Blob()
    ok = fn(
        ctypes.byref(data_in),
        None,  # description (protect) / ppszDataDescr (unprotect): unused
        ctypes.byref(entropy),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(data_out),
    )
    if not ok:
        raise DpapiError(f"{fn_name} failed (Windows error {ctypes.GetLastError()})")
    try:
        return ctypes.string_at(data_out.pbData, data_out.cbData)
    finally:
        kernel32.LocalFree(data_out.pbData)


def protect(data: bytes) -> bytes:
    if not available():
        raise DpapiError("DPAPI is only available on Windows")
    return _call("CryptProtectData", data)


def unprotect(blob: bytes) -> bytes:
    if not available():
        raise DpapiError("DPAPI is only available on Windows")
    return _call("CryptUnprotectData", blob)
