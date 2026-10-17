"""Show-events settings: navig config ``obs.show.*`` (env overrides for tests/ops).

| key                      | env                          | default                                      |
|--------------------------|------------------------------|----------------------------------------------|
| obs.show.enabled         | NAVIG_OBS_SHOW_ENABLED       | false                                        |
| obs.show.patch_feed      | NAVIG_OBS_SHOW_PATCH_FEED    | https://api.schema.cx/api/source/patches     |
| obs.show.poll_seconds    | NAVIG_OBS_SHOW_POLL_SECONDS  | 60 (floor 15)                                |
| obs.show.cues_file       | NAVIG_OBS_SHOW_CUES_FILE     | ~/.navig/obs-show/cues.json                  |
| obs.show.cue_input       | NAVIG_OBS_SHOW_CUE_INPUT     | SCHEMA · Cue                                 |
| obs.show.toast_event     | —                            | schema-toast                                 |
| obs.show.civic_feed      | NAVIG_OBS_SHOW_CIVIC_FEED    | https://api.schema.cx/api/civic/events       |
| obs.show.civic_event     | —                            | pigeonCivic                                  |
| obs.show.pigeon_event    | —                            | pigeon                                       |
| obs.show.civic_say       | NAVIG_OBS_SHOW_CIVIC_SAY     | false (TTS can bill per line)                |
| obs.show.deploy_say      | NAVIG_OBS_SHOW_DEPLOY_SAY    | false — a published patch is SPOKEN          |
| obs.show.say_on_air_only | NAVIG_OBS_SHOW_SAY_ON_AIR_ONLY | true — the bridge speaks only while streaming or recording |
| obs.show.say_voices      | NAVIG_OBS_SHOW_SAY_VOICES    | per-language voices (voices.DEFAULTS)        |
| obs.show.say_lang        | NAVIG_OBS_SHOW_SAY_LANG      | en (when the collection names no language)   |
| obs.show.say_provider    | NAVIG_OBS_SHOW_SAY_PROVIDER  | — (one provider for every language; legacy)  |
| obs.show.say_voice       | NAVIG_OBS_SHOW_SAY_VOICE     | — (with say_provider; legacy)                |
| obs.show.frames_watch    | NAVIG_OBS_SHOW_FRAMES_WATCH  | true (dropped frames → the pigeon's lag line)|
| obs.show.health_director | NAVIG_OBS_SHOW_HEALTH_DIRECTOR | false (sustained stream drops → BRB scene) |
| obs.show.brb_scene       | NAVIG_OBS_SHOW_BRB_SCENE     | 10 Signal Lost                               |

``say_voices`` maps a language to ``{provider, voice, style, fx}`` (see
``voices.py``); the stage's language (the loaded collection's ``(en|fr|ru)``)
picks the entry. ``say_provider``/``say_voice``, when set, force one voice for
every language — the old single-voice behaviour.

An empty ``patch_feed`` turns the deploy watcher off; the sale ring still works.
An empty ``civic_feed`` turns the Grid Pigeon's civic reactions off.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from navig_sdk.host import coerce_bool

DEFAULT_FEED = "https://api.schema.cx/api/source/patches"
DEFAULT_CUE_INPUT = "SCHEMA · Cue"
DEFAULT_TOAST_EVENT = "schema-toast"
DEFAULT_CIVIC_FEED = "https://api.schema.cx/api/civic/events"
DEFAULT_CIVIC_EVENT = "pigeonCivic"
DEFAULT_PIGEON_EVENT = "pigeon"
DEFAULT_SAY_PROVIDER = "edge"  # kept for callers; no longer a forced default
MIN_POLL_SECONDS = 15


def state_dir() -> Path:
    return Path.home() / ".navig" / "obs-show"


@dataclass(frozen=True)
class ShowSettings:
    enabled: bool = False
    patch_feed: str = DEFAULT_FEED
    poll_seconds: int = 60
    cues_file: Path = Path()
    cue_input: str = DEFAULT_CUE_INPUT
    toast_event: str = DEFAULT_TOAST_EVENT
    civic_feed: str = DEFAULT_CIVIC_FEED
    civic_event: str = DEFAULT_CIVIC_EVENT
    pigeon_event: str = DEFAULT_PIGEON_EVENT
    civic_say: bool = False
    deploy_say: bool = False
    say_on_air_only: bool = True
    say_provider: str | None = None
    say_voice: str | None = None
    say_voices: Any = None
    say_lang: str = "en"
    frames_watch: bool = True
    health_director: bool = False
    brb_scene: str = "10 Signal Lost"


def _cfg(key: str) -> str | None:
    try:
        from navig.config import get as cfg_get  # lazy: standalone has no navig

        value = cfg_get(key, None)
        return None if value is None else str(value)
    except Exception:  # noqa: BLE001 — standalone / unreadable config
        return None


def _cfg_raw(key: str) -> Any:
    """A config value as stored (a mapping stays a mapping)."""
    try:
        from navig.config import get as cfg_get  # lazy: standalone has no navig

        return cfg_get(key, None)
    except Exception:  # noqa: BLE001
        return None


def _pick(env: str | None, key: str) -> str | None:
    if env:
        raw = os.environ.get(env)
        if raw is not None:
            return raw
    return _cfg(key)


def load_settings() -> ShowSettings:
    poll_raw = _pick("NAVIG_OBS_SHOW_POLL_SECONDS", "obs.show.poll_seconds")
    try:
        poll = max(MIN_POLL_SECONDS, int(poll_raw)) if poll_raw else 60
    except ValueError:
        poll = 60
    feed = _pick("NAVIG_OBS_SHOW_PATCH_FEED", "obs.show.patch_feed")
    cues = _pick("NAVIG_OBS_SHOW_CUES_FILE", "obs.show.cues_file")
    civic = _pick("NAVIG_OBS_SHOW_CIVIC_FEED", "obs.show.civic_feed")
    voice = (_pick("NAVIG_OBS_SHOW_SAY_VOICE", "obs.show.say_voice") or "").strip()
    return ShowSettings(
        enabled=coerce_bool(_pick("NAVIG_OBS_SHOW_ENABLED", "obs.show.enabled"), False),
        patch_feed=DEFAULT_FEED if feed is None else feed.strip(),
        poll_seconds=poll,
        cues_file=Path(cues).expanduser() if cues else state_dir() / "cues.json",
        cue_input=_pick("NAVIG_OBS_SHOW_CUE_INPUT", "obs.show.cue_input")
        or DEFAULT_CUE_INPUT,
        toast_event=_cfg("obs.show.toast_event") or DEFAULT_TOAST_EVENT,
        civic_feed=DEFAULT_CIVIC_FEED if civic is None else civic.strip(),
        civic_event=_cfg("obs.show.civic_event") or DEFAULT_CIVIC_EVENT,
        pigeon_event=_cfg("obs.show.pigeon_event") or DEFAULT_PIGEON_EVENT,
        civic_say=coerce_bool(
            _pick("NAVIG_OBS_SHOW_CIVIC_SAY", "obs.show.civic_say"), False
        ),
        deploy_say=coerce_bool(
            _pick("NAVIG_OBS_SHOW_DEPLOY_SAY", "obs.show.deploy_say"), False
        ),
        say_on_air_only=coerce_bool(
            _pick("NAVIG_OBS_SHOW_SAY_ON_AIR_ONLY", "obs.show.say_on_air_only"), True
        ),
        say_provider=(
            _pick("NAVIG_OBS_SHOW_SAY_PROVIDER", "obs.show.say_provider") or ""
        ).strip()
        or None,
        say_voice=voice or None,
        say_voices=os.environ.get("NAVIG_OBS_SHOW_SAY_VOICES")
        or _cfg_raw("obs.show.say_voices"),
        say_lang=(_pick("NAVIG_OBS_SHOW_SAY_LANG", "obs.show.say_lang") or "en")
        .strip()
        .lower()
        or "en",
        frames_watch=coerce_bool(
            _pick("NAVIG_OBS_SHOW_FRAMES_WATCH", "obs.show.frames_watch"), True
        ),
        # Opt-in: it changes the scene ON AIR by itself.
        health_director=coerce_bool(
            _pick("NAVIG_OBS_SHOW_HEALTH_DIRECTOR", "obs.show.health_director"), False
        ),
        brb_scene=(_pick("NAVIG_OBS_SHOW_BRB_SCENE", "obs.show.brb_scene") or "10 Signal Lost").strip()
        or "10 Signal Lost",
    )
