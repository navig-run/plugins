"""The live numbers a channel's cards quote — fetched loud, cached, never silently stale.

Ported from the workspace's ``live.py``. The cards used to carry hand-typed figures,
which go stale silently — the worst failure mode for a channel whose whole promise is
that the numbers are real. This reads them at build time instead.

The endpoints and the cache path come from ``<workspace>/channel.json`` (so the shape
of the answer stays workspace code — this module only guarantees the contract):
public read → disk cache → loud failure. A fetch failure falls back to the last good
answer ONLY if the caller allows it, and the answer then says ``_source: cache`` and
its age. ``--strict`` refuses instead.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

__all__ = ["DataError", "LiveNumbers", "fetch"]

DEFAULT_TIMEOUT = 20
USER_AGENT = "navig-channel-data/1.0"


class DataError(RuntimeError):
    """A fetch failed and there is no acceptable fallback — the message says what to do."""


class LiveNumbers(dict):
    """The shaped answer plus ``_source`` (``live`` | ``cache``) and its age."""

    @property
    def source(self) -> str:
        return str(self.get("_source", "live"))


def _cache_path(ws: Path) -> Path:
    from navig_text.workspace import channel_config

    cfg = channel_config(ws)
    name = str((cfg.get("data") or {}).get("cache") or ".live-cache.json")
    return Path(ws) / name


def _get(url: str, timeout: int) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — public API, https
        return json.loads(r.read().decode("utf-8"))


def fetch(
    ws: Path, strict: bool = False, timeout: int = DEFAULT_TIMEOUT
) -> LiveNumbers:
    """Return the numbers the cards need. Never silently stale.

    ``strict=True`` raises :class:`DataError` on any fetch failure instead of
    falling back to the cache; without a cache a failure always raises.
    """
    ws = Path(ws)
    cache = _cache_path(ws)
    from navig_text.workspace import channel_config

    cfg = (channel_config(ws).get("data") or {})
    api = str(cfg.get("api") or "https://api.schema.cx")
    endpoints = cfg.get("endpoints") or ["/api/landrun/summary", "/api/landrun"]

    try:
        payloads = [_get(api + path, timeout) for path in endpoints]
        shaped = _run_shape(ws, cfg, payloads)
        shaped["_source"] = "live"
        shaped["_fetchedAt"] = datetime.now(timezone.utc).isoformat()
        cache.write_text(
            json.dumps(shaped, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return LiveNumbers(shaped)
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        if strict:
            raise DataError(
                f"live fetch failed and --strict is set: {exc}. "
                "Drop --strict to use the cached answer, or fix the network."
            ) from exc
        if cache.is_file():
            data = json.loads(cache.read_text(encoding="utf-8"))
            age = time.time() - cache.stat().st_mtime
            data["_source"] = "cache"
            data["_ageHours"] = round(age / 3600, 1)
            return LiveNumbers(data)
        raise DataError(
            f"live fetch failed and no cache exists: {exc}. "
            "Run once with network to create the cache, then --strict will have a floor."
        ) from exc


def _run_shape(ws: Path, cfg: dict, payloads: list[dict]) -> dict:
    """Shape the raw endpoint payloads: ``data.shape`` (``module:function``) or built-in.

    ``data.shape`` names a function IN THE WORKSPACE (``"live:raw_shape"``), so a
    channel whose endpoints are not summary/board keeps its own semantics while this
    module keeps the fetch/cache/strict contract.
    """
    spec = cfg.get("shape")
    if spec:
        mod_name, _, func_name = str(spec).partition(":")
        if not func_name:
            raise DataError(
                f"data.shape must be 'module:function', got {spec!r} in channel.json"
            )
        from navig_text.workspace import load_workspace_module

        mod = load_workspace_module(ws, mod_name)
        fn = getattr(mod, func_name, None)
        if fn is None:
            raise DataError(f"data.shape {spec!r}: {mod_name}.py has no {func_name}")
        shaped = fn(*payloads)
        if not isinstance(shaped, dict):
            raise DataError(f"data.shape {spec!r} must return a dict, got {type(shaped)}")
        return shaped
    if len(payloads) != 2:
        raise DataError(
            f"built-in shape expects 2 endpoints (summary, board), got {len(payloads)} "
            "— set data.shape in channel.json"
        )
    return _shape(*payloads)


def _shape(summary: dict, board: dict) -> dict:
    """The card-facing shape — workspace semantics, kept identical to live.py's.

    Kept here (not in the workspace) so ``navig text channel data`` works on any
    workspace whose endpoints follow the same summary/board shape; a workspace with
    a different shape overrides ``data.shape`` in channel.json with a dotted
    module:function to call instead.
    """
    zones = {z["slug"]: z for z in (board.get("zones") or [])}
    wave = summary.get("wave") or {}
    ends = wave.get("endsAt")
    days = None
    if ends:
        try:
            end = datetime.fromisoformat(ends.replace("Z", "+00:00"))
            days = max(0, (end - datetime.now(timezone.utc)).days)
        except ValueError:
            pass
    mpl = zones.get("montpellier") or {}
    lead = None
    for z in zones.values():
        if z.get("ignitedAt"):
            continue
        v = z.get("verified") or 0
        if v and (lead is None or v > (lead.get("verified") or 0)):
            lead = z
    return {
        "pioneers": summary.get("pioneers"),
        "ignited": summary.get("ignited"),
        "mayors": summary.get("mayors"),
        "citiesOpen": summary.get("citiesOpen"),
        "citiesTotal": summary.get("citiesTotal"),
        "seatsToMayor": summary.get("seatsToMayor"),
        "waveNumber": wave.get("number"),
        "waveTitle": wave.get("title"),
        "waveEndsAt": ends,
        "daysLeft": days,
        "doorDate": _door(ends),
        "founderNext": (summary.get("pioneers") or 0) + 1,
        "mpl": {
            "name": mpl.get("name", "Montpellier"),
            "verified": mpl.get("verified", 0),
            "toMayor": mpl.get("toMayor", 8),
        },
        "lead": {
            "name": (lead or mpl).get("name", "Montpellier"),
            "verified": (lead or mpl).get("verified", 0),
            "toMayor": (lead or mpl).get("toMayor", 8),
            "lat": (lead or mpl).get("lat"),
            "lng": (lead or mpl).get("lng"),
        },
        "citiesDark": (summary.get("citiesTotal") or 0) - (summary.get("ignited") or 0),
    }


def _door(ends: Optional[str]) -> str:
    if not ends:
        return "1 January"
    try:
        d = datetime.fromisoformat(ends.replace("Z", "+00:00"))
        return f"{d.day} {d.strftime('%B')}"
    except ValueError:
        return "1 January"
