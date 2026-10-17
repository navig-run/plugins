"""Stage 2 — many emails' items → one ranked briefing.

* **Merge** the same item seen in several emails (one entry, every source kept).
* **Forget** what an earlier briefing already delivered (by fingerprint) — unless it changed in a
  way that matters (an event's date or registration status).
* **Score** with reasons a person can read: which configured interest it matches, whether a job's
  place and day rate fit. No model is asked; an interest list in the space config is the input.
* **Rank** and cap each section, so a briefing stays a page, not a feed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from navig_email.brief.model import Item, normalize

SECTION_ORDER = ("event", "job", "product", "article")
SECTION_TITLES = {"product": "TECH DISCOVERIES", "event": "EVENTS", "job": "OPPORTUNITIES", "article": "READING"}
DEFAULT_CAPS = {"product": 5, "event": 5, "job": 6, "article": 3}


@dataclass
class Interests:
    topics: dict[str, list[str]] = field(default_factory=dict)  # label → keywords
    job_keywords: list[str] = field(default_factory=list)
    job_locations: list[str] = field(default_factory=list)
    min_day_rate: int = 0

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> Interests:
        jobs = cfg.get("jobs") if isinstance(cfg.get("jobs"), dict) else {}
        topics = cfg.get("interests") if isinstance(cfg.get("interests"), dict) else {}
        return cls(
            topics={str(k): [str(w).lower() for w in (v or [])] for k, v in topics.items()},
            job_keywords=[str(w).lower() for w in jobs.get("keywords") or []],
            job_locations=[normalize(str(w)) for w in jobs.get("locations") or []],
            min_day_rate=int(jobs.get("min_day_rate") or 0),
        )


def merge(items: list[Item]) -> list[Item]:
    """One entry per fingerprint, sources united. Order of first appearance is kept."""
    out: dict[str, Item] = {}
    for it in items:
        fp = it.fingerprint()
        if fp in out:
            out[fp].merge(it)
        else:
            out[fp] = it
    return list(out.values())


def _text(it: Item) -> str:
    f = it.fields
    parts = [it.title, it.summary, str(f.get("description") or ""), " ".join(f.get("features") or []),
             str(f.get("category") or ""), " ".join(f.get("topics") or [])]
    return " ".join(parts).lower()


def score(it: Item, interests: Interests, *, today: date) -> Item:
    reasons: list[str] = []
    s = {"event": 0.6, "job": 0.4, "product": 0.3, "article": 0.1}.get(it.kind, 0.0)
    text = _text(it)
    for label, words in interests.topics.items():
        hits = [w for w in words if w and w in text]
        if hits:
            s += 0.3 + 0.05 * (len(hits) - 1)
            reasons.append(label)
    if it.kind == "product" and it.fields.get("list") == "editorial":
        s += 0.2
    if it.kind == "event":
        try:
            when = date.fromisoformat(str(it.fields.get("date")))
        except ValueError:
            when = None
        if when is not None:
            days = (when - today).days
            if days < 0:
                return _with(it, -1.0, ["already happened"])
            s += 0.3 if days <= 14 else 0.1 if days <= 60 else 0.0
    if it.kind == "job":
        title = it.title.lower()
        kw = [w for w in interests.job_keywords if w in title]
        if kw:
            s += 0.2 * len(kw)
            reasons.append("matches " + ", ".join(kw))
        loc = normalize(str(it.fields.get("location") or ""))
        if interests.job_locations and any(l and l in loc for l in interests.job_locations):
            s += 0.2
            reasons.append(f"in {it.fields.get('location')}")
        rate = it.fields.get("day_rate") or {}
        top = rate.get("max") or rate.get("min")
        if interests.min_day_rate and top:
            if top >= interests.min_day_rate:
                s += 0.2
                reasons.append(f"day rate up to €{top} (≥ €{interests.min_day_rate})")
            else:
                s -= 0.2
                reasons.append(f"day rate below €{interests.min_day_rate}")
    return _with(it, round(s, 3), reasons)


def _with(it: Item, s: float, reasons: list[str]) -> Item:
    it.score, it.reasons = s, reasons
    return it


def _material(old: dict[str, Any], new: Item) -> bool:
    """An already-delivered item worth repeating: an event whose date or registration changed."""
    if new.kind != "event":
        return False
    f = old.get("fields") or {}
    return any(str(f.get(k)) != str(new.fields.get(k)) for k in ("date", "start", "registration"))


def drop_delivered(items: list[Item], store) -> list[Item]:
    keep = []
    for it in items:
        row = store._read_one("SELECT status, data_json FROM digest_items WHERE item_id = ?", (it.fingerprint(),))
        if row is None or row["status"] == "new":
            keep.append(it)
        elif row["status"] == "delivered" and _material(json.loads(row["data_json"] or "{}"), it):
            it.reasons.append("updated since the last briefing")
            keep.append(it)
    return keep


def select(items: list[Item], caps: dict[str, int] | None = None) -> dict[str, list[Item]]:
    caps = {**DEFAULT_CAPS, **(caps or {})}
    out: dict[str, list[Item]] = {}
    for kind in SECTION_ORDER:
        ranked = sorted((i for i in items if i.kind == kind and i.score >= 0), key=lambda i: (-i.score, i.title))
        if kind == "event":  # soonest first among the kept ones
            ranked = sorted(ranked[: caps[kind]], key=lambda i: str(i.fields.get("date") or "9999"))
        if ranked[: caps[kind]]:
            out[kind] = ranked[: caps[kind]]
    return out


def synthesize(items: list[Item], *, interests: Interests, today: date, store, caps: dict[str, int] | None = None
               ) -> dict[str, list[Item]]:
    merged = merge(items)
    fresh = drop_delivered(merged, store)
    return select([score(i, interests, today=today) for i in fresh], caps)


def count(sections: dict[str, list[Item]]) -> int:
    return sum(len(v) for v in sections.values())
