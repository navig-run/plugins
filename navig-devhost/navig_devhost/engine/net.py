"""Network helpers — loopback allocation and reachability checks."""

from __future__ import annotations

import re
import socket

_LOOPBACK_RE = re.compile(r"\b127\.0\.0\.(\d{1,3})\b")


def loopbacks_in_use(hosts_content: str, *extra_ips: str) -> set[str]:
    """Every 127.0.0.x already claimed in the hosts file (plus any extras)."""
    used = {f"127.0.0.{m.group(1)}" for m in _LOOPBACK_RE.finditer(hosts_content or "")}
    used.update(ip for ip in extra_ips if ip)
    used.add("127.0.0.1")  # standard localhost — never hand it out
    return used


def next_free_loopback(hosts_content: str, *extra_ips: str) -> str:
    """Pick the next unclaimed 127.0.0.x (2..254). Matches the house pattern of a
    dedicated loopback per site so each can bind :443 without colliding."""
    used = loopbacks_in_use(hosts_content, *extra_ips)
    for octet in range(2, 255):
        ip = f"127.0.0.{octet}"
        if ip not in used:
            return ip
    raise RuntimeError("no free 127.0.0.x loopback available (2..254 all used)")


def target_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    """True if something is listening on host:port (the dev server)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def can_bind(host: str, port: int) -> bool:
    """True if we can bind host:port right now (proxy not already running / free)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()
