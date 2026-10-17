"""Where devhost keeps its state (registry + certs), under navig's config dir."""

from __future__ import annotations

from pathlib import Path


def devhost_dir() -> Path:
    """`<navig config>/devhost` — the same directory with or without navig (navig-sdk)."""
    from navig_sdk.host import config_dir

    d = Path(config_dir()) / "devhost"
    d.mkdir(parents=True, exist_ok=True)
    return d


def registry_path() -> Path:
    return devhost_dir() / "registry.json"


def certs_dir() -> Path:
    d = devhost_dir() / "certs"
    d.mkdir(parents=True, exist_ok=True)
    return d
