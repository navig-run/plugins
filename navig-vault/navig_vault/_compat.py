"""Compatibility seam — use navig's implementations when navig is installed, else fall back.

navig-vault must install and run **without** navig, so every navig-internal dependency the
vault engine needs is routed through this one module. Same shape as
``navig_blackbox/_compat.py``, which is the working precedent in this repo.

**Delegate first, always.** In a navig install the ``try`` branch is the one that runs, so
the fallback below never executes there and the two cannot diverge in practice. That
ordering is the whole safety property: a fallback that only runs standalone is a fallback
nobody exercises, and the sibling ``navig_blackbox/_compat.py`` already carries a real bug
of exactly that kind (its ``_config_dir_fallback`` forgets the system-service branch, so a
root-run service resolves the wrong directory).

Two consequences of that, deliberately:

* :func:`set_owner_only_file_permissions` is a **verbatim** copy of
  ``navig.core.file_permissions``. It guards ``vault.db`` (+ its ``-wal``/``-shm``
  companions) and ``vault.salt``; if it degrades, the credential inventory and audit log
  become readable to other local users. There is no reason for the standalone copy to be
  weaker, so it is byte-equivalent rather than "close enough", and a parity test in core
  fails the build if the two drift.
* :func:`vault_dir` mirrors ``navig.platform.paths`` **branch for branch**, including the
  system-service case. navig and a standalone install must resolve the SAME directory or
  the user silently ends up with two vaults.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Mirrors navig.core.yaml_io. The values are duplicated rather than imported because this
# module must work with navig ABSENT -- that is its entire purpose. The parity test in
# navig's suite asserts they still match, so a change to core's retry policy that is not
# reflected here fails the build rather than silently leaving the standalone vault less
# resilient than the navig-backed one.
_ATOMIC_REPLACE_RETRIES = 3
_ATOMIC_REPLACE_BACKOFF_BASE = 0.05

_logger = logging.getLogger(__name__)


# ── file permissions ─────────────────────────────────────────────────────────


def _set_owner_only_fallback(path: str | Path) -> None:
    """Verbatim copy of ``navig.core.file_permissions.set_owner_only_file_permissions``.

    Kept byte-equivalent on purpose — see the module docstring. In particular the Windows
    branch must keep the ``/inheritance:r`` + ``/grant:r`` + ``/remove:g`` triple, and must
    NOT pass ``text=True``: icacls writes localised group names in the OEM code page, and
    under Python's UTF-8 mode that decode raises inside subprocess's reader THREAD — run()
    returns normally and the traceback is printed by a background thread nobody watches.
    """
    target = str(path)

    if os.name != "nt":
        try:
            os.chmod(target, 0o600)
        except (OSError, PermissionError):
            pass
        return

    try:
        import getpass  # noqa: PLC0415
        import subprocess  # noqa: PLC0415
    except ImportError:
        _logger.debug("Windows ACL setup skipped because required modules are unavailable")
        return

    # Three icacls calls per secured file, on every credential/token write. From a
    # windowless parent (navig's daemon runs under pythonw.exe) each one flashed a console
    # window at the operator. The flag is read off `subprocess` rather than imported from
    # navig.platform.process precisely so this vendored copy still works with navig ABSENT.
    # Everything below is unconditionally Windows — the non-nt branch returned above — so
    # the attribute always exists and `creationflags` is always legal.
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        username = getpass.getuser()
        subprocess.run(
            ["icacls", target, "/inheritance:r"],
            capture_output=True,
            check=False,
            creationflags=no_window,
        )
        subprocess.run(
            ["icacls", target, "/grant:r", f"{username}:(R,W)"],
            capture_output=True,
            check=False,
            creationflags=no_window,
        )
        subprocess.run(
            ["icacls", target, "/remove:g", "Users", "Authenticated Users", "Everyone"],
            capture_output=True,
            check=False,
            creationflags=no_window,
        )
    except (OSError, PermissionError, subprocess.SubprocessError):
        _logger.debug("Windows ACL setup failed for %s", target, exc_info=True)


def set_owner_only_file_permissions(path: str | Path) -> None:
    """Owner-only permissions on a vault file. Best-effort; never raises."""
    try:
        from navig.core.file_permissions import (  # noqa: PLC0415
            set_owner_only_file_permissions as _impl,
        )

        _impl(path)
    except ImportError:
        _set_owner_only_fallback(path)


# ── atomic text write ────────────────────────────────────────────────────────


def atomic_write_text(path: Path | str, content: str, *, encoding: str = "utf-8") -> None:
    """Crash-safe text write (temp file in the same directory + atomic replace)."""
    try:
        from navig.core.yaml_io import atomic_write_text as _impl  # noqa: PLC0415

        _impl(path, content, encoding=encoding)
        return
    except ImportError:
        pass

    _atomic_write_fallback(path, content, encoding=encoding)


def _atomic_write_fallback(path: Path | str, content: str, *, encoding: str = "utf-8") -> None:
    """Standalone crash-safe write. Exposed so tests can exercise it directly.

    Splitting every fallback out of its delegating wrapper is deliberate: the alternative
    is a fixture that evicts navig from ``sys.modules`` to force this branch, and a
    hand-rolled eviction is banned here — a module imported inside one outlives teardown
    and breaks dotted ``monkeypatch.setattr`` for every later test in the same xdist worker.
    Testing the fallback directly needs no eviction at all.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(_ATOMIC_REPLACE_RETRIES):
        # Same directory as the destination, so the replace stays on one filesystem and is
        # actually atomic rather than a copy.
        fd, tmp_name = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.tmp", suffix=".navig~")
        try:
            with os.fdopen(fd, "w", encoding=encoding) as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, p)
            return
        except PermissionError:
            # Windows: a transient lock held by antivirus or a backup agent. navig's own
            # writer retries this and the fallback did not, so a STANDALONE vault raised
            # where a navig-backed one succeeded -- on a secrets store, and only on the
            # platform where it happens. Retry on the last attempt is pointless and on
            # POSIX a PermissionError is a real permission problem, so neither retries.
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            if attempt == _ATOMIC_REPLACE_RETRIES - 1 or sys.platform != "win32":
                raise
            time.sleep(_ATOMIC_REPLACE_BACKOFF_BASE * (attempt + 1))
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


# ── json ─────────────────────────────────────────────────────────────────────


def safe_json_loads(raw: object, default: Any) -> Any:
    """Parse a stored JSON blob, degrading NULL/empty/malformed to *default*.

    Read-side degrade only: stores parse JSON columns inside fetch loops, so a bare
    ``json.loads`` raising on ONE corrupt row loses the entire fetch rather than the bad
    blob. Nothing writes *default* back over the original.
    """
    try:
        from navig.core.json_io import safe_json_loads as _impl  # noqa: PLC0415

        return _impl(raw, default)
    except ImportError:
        pass

    return _safe_json_loads_fallback(raw, default)


def _safe_json_loads_fallback(raw: object, default: Any) -> Any:
    """Standalone JSON degrade. Exposed for direct testing (see _atomic_write_fallback)."""
    if not raw:
        return default
    try:
        return json.loads(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


# ── paths ────────────────────────────────────────────────────────────────────


def _is_system_service_fallback() -> bool:
    """Mirror of ``navig.platform.paths._is_system_service``.

    All three legs matter. Dropping them is the live bug in navig_blackbox's ``_compat``:
    a root-run systemd service resolves ``/root/.navig`` instead of ``/etc/navig`` and
    silently opens a DIFFERENT, empty vault.
    """
    if os.environ.get("NAVIG_SYSTEM_SERVICE") == "1":
        return True
    if os.name == "nt":
        return False
    try:
        import pwd  # noqa: PLC0415

        if pwd.getpwuid(os.getuid()).pw_name == "navig":  # type: ignore[attr-defined]
            return True
    except (ImportError, KeyError, AttributeError, OSError):
        pass
    try:
        return bool(os.environ.get("INVOCATION_ID")) and os.getuid() == 0  # type: ignore[attr-defined]
    except AttributeError:
        return False


def config_dir() -> Path:
    """navig's config dir, or the same default resolved standalone."""
    try:
        from navig.platform.paths import config_dir as _impl  # noqa: PLC0415

        return _impl()
    except ImportError:
        pass

    return _config_dir_fallback()


def _config_dir_fallback() -> Path:
    """Standalone config dir, branch-for-branch with navig.platform.paths.config_dir."""
    env = os.environ.get("NAVIG_CONFIG_DIR")
    if env:
        return Path(env)
    if _is_system_service_fallback():
        return Path("/etc/navig")
    return Path.home() / ".navig"


def vault_dir() -> Path:
    """The vault directory — the SAME one navig resolves.

    Resolution order, matching ``navig.platform.paths.vault_dir``:
    ``NAVIG_VAULT_DIR`` → ``NAVIG_CONFIG_DIR/vault`` → system service ``/etc/navig/vault``
    → ``~/.navig/vault``. An explicit ``vault_dir=`` argument to ``Vault``/``get_vault``
    still wins over all of this; the coupling was only ever in the default.
    """
    try:
        from navig.platform.paths import vault_dir as _impl  # noqa: PLC0415

        return _impl()
    except ImportError:
        pass

    return _vault_dir_fallback()


def _vault_dir_fallback() -> Path:
    """Standalone vault dir, branch-for-branch with navig.platform.paths.vault_dir."""
    env = os.environ.get("NAVIG_VAULT_DIR")
    if env:
        return Path(env)
    return _config_dir_fallback() / "vault"


def navig_available() -> bool:
    """Is navig importable in this environment? (Used by tests and diagnostics.)"""
    try:
        import navig  # noqa: F401, PLC0415

        return True
    except ImportError:
        return False
