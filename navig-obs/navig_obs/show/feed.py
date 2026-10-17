"""Watch Schema's Source log feed and say what changed since the last look.

The feed (``GET /api/source/patches``, public) is already world-voiced: its
headlines passed Schema's brand guard, so the stream may show them as they are.
Two moments become toasts:

- ``compiling`` — a deploy landed: the open patch appeared, or gained changes.
- ``patch``     — a patch was published (a new ``seq``). After downtime only the
  NEWEST new patch fires — a restart never floods the screen.

The very first look only remembers where the feed is (no toast for history).
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

USER_AGENT = "navig-obs-show/1"


@dataclass(frozen=True)
class FeedState:
    seq: int = 0
    compiling_since: str | None = None
    compiling_count: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "compilingSince": self.compiling_since,
            "compilingCount": self.compiling_count,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> FeedState:
        return cls(
            seq=int(raw.get("seq") or 0),
            compiling_since=raw.get("compilingSince"),
            compiling_count=int(raw.get("compilingCount") or 0),
        )


def load_state(path: Path) -> FeedState | None:
    try:
        return FeedState.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def save_state(path: Path, state: FeedState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state.to_json()), encoding="utf-8")
    tmp.replace(path)


def fetch(url: str, timeout: float = 10.0) -> dict[str, Any]:
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — configured https URL
        doc = json.loads(resp.read().decode("utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("feed answered with a non-object")
    return doc


def diff(
    prev: FeedState | None, doc: dict[str, Any]
) -> tuple[list[dict[str, Any]], FeedState]:
    """(toasts to show, the new state). ``prev=None`` = first look: seed, no toasts."""
    patches = [
        p
        for p in (doc.get("patches") or [])
        if isinstance(p, dict) and isinstance(p.get("seq"), int)
    ]
    newest = max(patches, key=lambda p: p["seq"]) if patches else None
    compiling = doc.get("compiling") if isinstance(doc.get("compiling"), dict) else None

    seq = newest["seq"] if newest else (prev.seq if prev else 0)
    since = compiling.get("since") if compiling else None
    count = int(compiling.get("count") or 0) if compiling else 0
    state = FeedState(
        seq=max(seq, prev.seq if prev else 0),
        compiling_since=since,
        compiling_count=count,
    )
    if prev is None:
        return [], state

    toasts: list[dict[str, Any]] = []
    if compiling and (since != prev.compiling_since or count > prev.compiling_count):
        toasts.append({"kind": "compiling", "count": count, "since": since})
    if newest and newest["seq"] > prev.seq:
        toasts.append(
            {
                "kind": "patch",
                "seq": newest["seq"],
                "version": newest.get("version"),
                "headline": str(newest.get("headline") or "").strip(),
                "count": int(newest.get("publicCount") or 0),
                # The world layers the patch touched (keys; the speech names
                # them in the stage's language — voices.deploy_line).
                "layers": [
                    str(x.get("key"))
                    for x in (newest.get("layers") or [])
                    if isinstance(x, dict) and x.get("key")
                ],
            }
        )
    return toasts, state
