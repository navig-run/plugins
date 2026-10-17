"""Resolve the OBS websocket target: host, port, password.

Precedence (first hit wins), per field:

1. explicit CLI flag (group callback),
2. env (``NAVIG_OBS_HOST`` / ``NAVIG_OBS_PORT`` / ``NAVIG_OBS_PASSWORD``),
3. navig config ``obs.host`` / ``obs.port`` / ``obs.password`` (guarded import —
   this plugin runs standalone without navig),
4. the local OBS websocket config file (loopback hosts only) for the password,
5. defaults (127.0.0.1:4455, no password).

The password is NEVER logged, printed, or included in error messages.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4455
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


@dataclass(frozen=True)
class ObsTarget:
    """Where to connect, and how to authenticate."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    password: str | None = None
    password_source: str = "none"  # where the password came from (never the value)


def local_obs_websocket_config() -> dict | None:
    """Read OBS's own websocket config file on THIS machine (best effort).

    Returns the parsed dict, or None when absent/unreadable. The dict contains
    ``server_password`` — callers must never print it.
    """
    for path in _local_config_paths():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _local_config_paths() -> list[Path]:
    paths: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.append(Path(appdata) / "obs-studio" / "plugin_config" / "obs-websocket" / "config.json")
    paths.append(
        Path.home() / "Library" / "Application Support" / "obs-studio" / "plugin_config" / "obs-websocket" / "config.json"
    )
    paths.append(Path.home() / ".config" / "obs-studio" / "plugin_config" / "obs-websocket" / "config.json")
    return paths


def _navig_cfg(key: str, default: str | None = None) -> str | None:
    """Read a navig config key; None when navig is absent (standalone mode)."""
    try:
        from navig.config import get as cfg_get  # lazy: may not exist standalone

        value = cfg_get(key, default)
        return str(value) if value is not None else default
    except Exception:  # noqa: BLE001 — standalone / no config: fall through
        return default


def resolve_target(
    host: str | None = None,
    port: int | None = None,
    password: str | None = None,
) -> ObsTarget:
    """Resolve the connection target per the module docstring's precedence."""
    resolved_host = host or os.environ.get("NAVIG_OBS_HOST") or _navig_cfg("obs.host") or DEFAULT_HOST

    resolved_port = port
    if resolved_port is None:
        env_port = os.environ.get("NAVIG_OBS_PORT")
        if env_port:
            try:
                resolved_port = int(env_port)
            except ValueError:
                resolved_port = None
    if resolved_port is None:
        cfg_port = _navig_cfg("obs.port")
        if cfg_port:
            try:
                resolved_port = int(cfg_port)
            except ValueError:
                resolved_port = None
    if resolved_port is None:
        resolved_port = DEFAULT_PORT

    resolved_password: str | None = None
    password_source = "none"
    if password is not None:
        resolved_password, password_source = password, "flag"
    else:
        env_password = os.environ.get("NAVIG_OBS_PASSWORD")
        if env_password:
            resolved_password, password_source = env_password, "env:NAVIG_OBS_PASSWORD"
        else:
            cfg_password = _navig_cfg("obs.password")
            if cfg_password:
                resolved_password, password_source = cfg_password, "config:obs.password"
            elif resolved_host in _LOOPBACK:
                obs_cfg = local_obs_websocket_config()
                if obs_cfg and obs_cfg.get("server_password"):
                    resolved_password = str(obs_cfg["server_password"])
                    password_source = "local-obs-config"

    return ObsTarget(
        host=resolved_host,
        port=resolved_port,
        password=resolved_password,
        password_source=password_source,
    )
