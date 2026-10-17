"""Which side of the desk a space sits on: the company's paperwork, or the person's.

``business`` (the default, and the only behaviour this plugin had until now) files
invoices/quotes/contracts/tax and hands the person's documents off. ``personal``
inverts that: the person's administrative mail — CAF, CPAM, bail, préfecture, EDF —
is what gets filed, into ``personal/<bucket>/``, and the company's paperwork is what
gets handed off.

A space declares its profile once, in ``.navig/config.yaml``::

    paperwork:
      profile: personal

so cron jobs and the Telegram intake never have to remember a flag. ``--profile`` on
the command line still wins, for one-off runs.
"""

from __future__ import annotations

from pathlib import Path

PROFILES: tuple[str, ...] = ("business", "personal")
DEFAULT_PROFILE = "business"


def read_space_config(space_root: Path) -> dict:
    """The space's ``.navig/config.yaml`` as a dict; ``{}`` when absent or unreadable."""
    path = space_root / ".navig" / "config.yaml"
    if not path.exists():
        return {}
    try:
        from navig_sdk.files import safe_load_yaml  # navig's reader, or its twin standalone

        data = safe_load_yaml(path)
    except Exception:  # noqa: BLE001 — a broken config means "no declared profile"
        return {}
    return data if isinstance(data, dict) else {}


def resolve_profile(space_root: Path, explicit: str | None = None) -> str:
    """``--profile`` if given, else the space's declared profile, else ``business``.

    Raises ``ValueError`` on an unknown name so the CLI can turn it into a usage error
    instead of silently filing everything as business paperwork.
    """
    name = (explicit or "").strip().lower()
    if not name:
        cfg = read_space_config(space_root)
        declared = (
            (cfg.get("paperwork") or {}).get("profile")
            if isinstance(cfg, dict)
            else None
        )
        name = str(declared or DEFAULT_PROFILE).strip().lower()
    if name not in PROFILES:
        raise ValueError(
            f"unknown paperwork profile {name!r} (expected one of {', '.join(PROFILES)})"
        )
    return name


def renewal_alert_days(space_root: Path, default: int = 30) -> int:
    """The space's ``renewal_alert_days`` — the horizon of the deadline radar."""
    cfg = read_space_config(space_root)
    try:
        value = int(cfg.get("renewal_alert_days", default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default
