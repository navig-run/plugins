"""Where devhost keeps its state (registry + certs), under navig's config dir."""

from __future__ import annotations

from pathlib import Path


def devhost_dir() -> Path:
    """`<navig config>/devhost`, falling back to `~/.navig/devhost` if navig's
    paths module isn't importable for some reason."""
    try:
        from navig.platform.paths import config_dir

        base = Path(config_dir())
    except Exception:  # pragma: no cover - defensive fallback
        base = Path.home() / ".navig"
    d = base / "devhost"
    d.mkdir(parents=True, exist_ok=True)
    return d


def registry_path() -> Path:
    return devhost_dir() / "registry.json"


def certs_dir() -> Path:
    d = devhost_dir() / "certs"
    d.mkdir(parents=True, exist_ok=True)
    return d
