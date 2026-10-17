"""System hosts-file entries for devhost domains.

Reuses navig's own hosts plumbing (`navig.local_operations.get_local_ops()`) for
the file path, admin check, and read — so devhost entries live in the same file
`navig hosts view` shows. devhost tags the lines it owns with a trailing
``# navig-devhost`` marker so it can add *and* remove them idempotently (plain
`navig hosts add` has no remove).
"""

from __future__ import annotations

from dataclasses import dataclass

TAG = "# navig-devhost"


@dataclass
class HostsResult:
    ok: bool
    message: str


def _ops():
    """navig's hosts plumbing when navig is installed, else the same three operations here."""
    try:
        from navig.local_operations import get_local_ops
    except ImportError:
        return _StandaloneHostsOps()
    return get_local_ops()


class _StandaloneHostsOps:
    """The three hosts-file operations devhost uses, mirroring navig's OS adapters.

    Path, admin check and read — including the exact failure strings ``read()`` above
    filters, so a failed read can never be mistaken for hosts-file content.
    """

    def get_hosts_file_path(self):
        import sys
        from pathlib import Path

        if sys.platform == "win32":
            return Path(r"C:\Windows\System32\drivers\etc\hosts")
        return Path("/etc/hosts")

    def can_edit_hosts_file(self) -> bool:
        import os
        import sys

        if sys.platform == "win32":
            try:
                import ctypes

                return ctypes.windll.shell32.IsUserAnAdmin() != 0
            except Exception:
                return False
        return os.geteuid() == 0

    def read_hosts_file(self) -> str:
        hosts_path = self.get_hosts_file_path()
        try:
            return hosts_path.read_text(encoding="utf-8")
        except PermissionError:
            return f"Permission denied reading {hosts_path}"
        except FileNotFoundError:
            return f"Hosts file not found at {hosts_path}"


def hosts_path() -> str:
    return str(_ops().get_hosts_file_path())


def can_edit() -> bool:
    try:
        return bool(_ops().can_edit_hosts_file())
    except Exception:
        return False


def read() -> str:
    content = _ops().read_hosts_file()
    # read_hosts_file returns human sentinels on failure ("Permission denied …",
    # "Hosts file not found …") — never treat those as real hosts-file content.
    if isinstance(content, str) and (
        content.startswith("Permission denied") or content.startswith("Hosts file not found")
    ):
        return ""
    return content or ""


def entry_for(domain: str) -> str | None:
    """The IP currently mapped to `domain` in an active (non-comment) line, or None."""
    for line in read().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) >= 2 and domain in parts[1:]:
            return parts[0]
    return None


def add(ip: str, domain: str) -> HostsResult:
    """Add `ip domain # navig-devhost` (idempotent). Requires admin."""
    existing = entry_for(domain)
    if existing == ip:
        return HostsResult(True, f"{domain} already maps to {ip}")
    if existing and existing != ip:
        return HostsResult(
            False,
            f"{domain} already maps to {existing} in the hosts file (remove it or pick --ip {existing}).",
        )
    if not can_edit():
        return HostsResult(False, "admin privileges required to edit the hosts file")

    path = hosts_path()
    try:
        content = read()
        newline = "" if content.endswith("\n") or content == "" else "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{newline}{ip}\t{domain}\t{TAG}\n")
        return HostsResult(True, f"added {ip}\t{domain}")
    except PermissionError:
        return HostsResult(False, "permission denied — run in an elevated terminal (Administrator)")
    except OSError as exc:
        return HostsResult(False, f"failed to write hosts file: {exc}")


def remove(domain: str) -> HostsResult:
    """Remove any active line mapping `domain` (devhost-tagged or not). Requires admin."""
    # Read ONCE and both check and rewrite from the same snapshot, so the write can
    # never act on different content than the existence check saw (no TOCTOU window
    # in which a bad/empty read could truncate the system hosts file).
    lines = read().splitlines()
    if not any(_line_maps(ln, domain) for ln in lines):
        return HostsResult(True, f"{domain} not in hosts file")
    if not can_edit():
        return HostsResult(False, "admin privileges required to edit the hosts file")

    kept = [ln for ln in lines if not _line_maps(ln, domain)]
    path = hosts_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(("\n".join(kept) + "\n") if kept else "")
        return HostsResult(True, f"removed {domain} from hosts file")
    except PermissionError:
        return HostsResult(False, "permission denied — run in an elevated terminal (Administrator)")
    except OSError as exc:
        return HostsResult(False, f"failed to write hosts file: {exc}")


def _line_maps(line: str, domain: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return False
    parts = stripped.split()
    return len(parts) >= 2 and domain in parts[1:]
