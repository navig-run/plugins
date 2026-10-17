"""Watch Schema's civic feed and hand each new event to the stream's characters.

The feed (``GET /api/civic/events``, public) is the same one the Broadcast
stage polls: Zones founded and ignited, mayors elected, Waves closed, crossings
opened. Every row is already a public announcement with no caller in it, so it
may go to the overlay as it is — the Grid Pigeon reacts to its ``kind``.

Same contract as the patch feed:

- the very first look only remembers where the feed is (no event for history);
- the cursor (newest ``occurredAt``) never moves backwards;
- after downtime only the newest ``MAX_BURST`` events fire, oldest first — a
  restart never sends the bird into a ten-minute frenzy.

Why navig carries it rather than the page polling it: an OBS overlay is a
local file, its origin is ``null``, and the API's CORS list (rightly) does not
include it. Reading server-side and pushing through ``emit_event`` needs no
change to the API's security policy.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_BURST = 3


@dataclass(frozen=True)
class CivicState:
    since: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"since": self.since}

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> CivicState:
        since = raw.get("since")
        return cls(since=since if isinstance(since, str) and since else None)


def load_state(path: Path) -> CivicState | None:
    try:
        return CivicState.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def save_state(path: Path, state: CivicState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state.to_json()), encoding="utf-8")
    tmp.replace(path)


def feed_url(base: str, state: CivicState | None) -> str:
    """The first look asks for one row (just to find the cursor)."""
    q = (
        {"limit": "1"}
        if state is None or not state.since
        else {"since": state.since, "limit": "20"}
    )
    sep = "&" if "?" in base else "?"
    return base + sep + urllib.parse.urlencode(q)


# World-voiced lines for `obs.show.civic_say`, one per stage language (the
# loaded collection's — see voices.lang_of). {city} is the Zone's name in
# capitals; {persona} is a citizen's persona name, present on an arrival only
# when that citizen chose to be shown on Schema TV. A kind without a line (or a
# line needing an absent value) stays a silent beat. People and roles stay
# lower case mid-sentence (the world's casing rule); world terms keep theirs.
LINES: dict[str, dict[str, str]] = {
    "ignition": {
        "en": "{city} has ignited.",
        "fr": "La Zone {city} s'est allumée.",
        "ru": "Зона {city} зажглась.",
    },
    "mayor_elected": {
        "en": "{city} has a new mayor.",
        "fr": "La Zone {city} a un nouveau maire.",
        "ru": "У Зоны {city} новый мэр.",
    },
    "zone_founded": {
        "en": "{city} has been founded.",
        "fr": "La Zone {city} est fondée.",
        "ru": "Зона {city} основана.",
    },
    "crossing_opened": {
        "en": "A crossing opened in {city}.",
        "fr": "Un passage s'est ouvert dans la Zone {city}.",
        "ru": "В Зоне {city} открылся переход.",
    },
    "wave_closed": {
        "en": "The Wave has closed.",
        "fr": "La Vague s'est refermée.",
        "ru": "Волна закрылась.",
    },
    "broadcast_live": {
        "en": "The broadcast is live.",
        "fr": "L'émission est en direct.",
        "ru": "Эфир начался.",
    },
    "broadcast_ended": {
        "en": "The broadcast has ended.",
        "fr": "L'émission est terminée.",
        "ru": "Эфир окончен.",
    },
    "grounds_met": {
        "en": "{city} has met its grounds.",
        "fr": "La Zone {city} a réuni ses fondations.",
        "ru": "Зона {city} заложила свои основания.",
    },
    "framework_work": {
        "en": "Work on the framework moves in {city}.",
        "fr": "La Charpente avance dans la Zone {city}.",
        "ru": "В Зоне {city} растёт Каркас.",
    },
    "flash_live": {
        "en": "A flash mission opened in {city}.",
        "fr": "Une mission éclair s'ouvre dans la Zone {city}.",
        "ru": "В Зоне {city} появилась миссия-вспышка.",
    },
    "passport_forged": {
        "en": "A Passport was forged in {city}.",
        "fr": "Un Passeport a été forgé dans la Zone {city}.",
        "ru": "В Зоне {city} выкован Паспорт.",
    },
}

# An arrival whose citizen chose to be shown names them.
PERSONA_LINES: dict[str, dict[str, str]] = {
    "passport_forged": {
        "en": "{persona} arrived in {city}.",
        "fr": "{persona} a rejoint la Zone {city}.",
        "ru": "{persona} — теперь в Зоне {city}.",
    },
}

# The card's colour per kind (the overlay's tones); anything else is "info".
TONES: dict[str, str] = {
    "ignition": "live",
    "broadcast_live": "live",
    "flash_live": "game",
    "passport_forged": "passport",
}


def tone(ev: dict[str, Any]) -> str:
    return TONES.get(str(ev.get("kind") or ""), "info")


def city(ev: dict[str, Any]) -> str | None:
    zone = ev.get("zone")
    if isinstance(zone, dict):
        name = str(zone.get("name") or zone.get("slug") or "").strip()
        return name or None
    return None


def persona(ev: dict[str, Any]) -> str | None:
    payload = ev.get("payload")
    if isinstance(payload, dict):
        name = str(payload.get("personaName") or "").strip()
        return name[:60] or None
    return None


def line(ev: dict[str, Any], lang: str = "en") -> str | None:
    """A short world-voiced line to speak for ``ev`` in ``lang``, or None."""
    kind = str(ev.get("kind") or "")
    name = city(ev)
    who = persona(ev)
    for table, need_persona in ((PERSONA_LINES, True), (LINES, False)):
        if need_persona and not who:
            continue
        variants = table.get(kind)
        if not variants:
            continue
        template = variants.get(lang) or variants["en"]
        if "{city}" in template and not name:
            continue
        return template.format(city=(name or "").upper(), persona=who or "")
    return None


def diff(
    prev: CivicState | None, doc: dict[str, Any]
) -> tuple[list[dict[str, Any]], CivicState]:
    """(events to emit oldest-first, the new state). ``prev=None`` = seed only."""
    rows = [
        r
        for r in (doc.get("data") or [])
        if isinstance(r, dict)
        and isinstance(r.get("occurredAt"), str)
        and r.get("kind")
    ]
    newest = max((r["occurredAt"] for r in rows), default=None)
    old = prev.since if prev else None
    # ISO-8601 UTC strings from one server compare correctly as text.
    since = max(x for x in (newest, old) if x) if (newest or old) else None
    state = CivicState(since=since)
    if prev is None:
        return [], state
    fresh = sorted(
        (r for r in rows if not old or r["occurredAt"] > old),
        key=lambda r: r["occurredAt"],
    )
    return fresh[-MAX_BURST:], state
