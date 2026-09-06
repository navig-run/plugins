"""mkcert wrapper — trusted local TLS certificates for devhost domains.

mkcert installs a local CA into the OS trust store, so certs it issues are trusted
by browsers with no warnings. devhost shells out to it (no Python TLS-CA code) and
stores per-domain certs under `<navig config>/devhost/certs/`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from navig.core.proc_text import decode_console_result
from navig_devhost.engine.paths import certs_dir

# WinGet drops shims here without always being on PATH mid-session.
_WINGET_LINK = os.path.expandvars(r"%LocalAppData%\Microsoft\WinGet\Links\mkcert.exe")


@dataclass
class CertResult:
    ok: bool
    cert: str | None
    key: str | None
    message: str


def find_mkcert() -> str | None:
    found = shutil.which("mkcert")
    if found:
        return found
    if Path(_WINGET_LINK).exists():
        return _WINGET_LINK
    return None


def caroot() -> Path | None:
    exe = find_mkcert()
    if not exe:
        return None
    try:
        out = decode_console_result(subprocess.run([exe, "-CAROOT"], capture_output=True, timeout=10))
        root = Path(out.stdout.strip())
        return root if root.exists() else None
    except Exception:
        return None


def ca_installed() -> bool:
    """Best-effort: the local CA has been generated (and, we assume, `mkcert -install`ed)."""
    root = caroot()
    return bool(root and (root / "rootCA.pem").exists())


def cert_paths(domain: str) -> tuple[Path, Path]:
    d = certs_dir()
    return d / f"{domain}.pem", d / f"{domain}-key.pem"


def generate(domain: str, ip: str) -> CertResult:
    exe = find_mkcert()
    if not exe:
        return CertResult(False, None, None,
                          "mkcert not found — install it (winget install FiloSottile.mkcert) and run `mkcert -install`.")
    cert, key = cert_paths(domain)
    names = [domain, ip] if ip and ip != domain else [domain]
    try:
        proc = decode_console_result(subprocess.run(
            [exe, "-cert-file", str(cert), "-key-file", str(key), *names],
            capture_output=True, timeout=60,
        ))
    except Exception as exc:  # noqa: BLE001
        return CertResult(False, None, None, f"mkcert failed to run: {exc}")
    if proc.returncode != 0 or not cert.exists() or not key.exists():
        detail = (proc.stderr or proc.stdout or "unknown error").strip()
        return CertResult(False, None, None, f"mkcert error: {detail}")
    return CertResult(True, str(cert), str(key), f"issued cert for {', '.join(names)}")
