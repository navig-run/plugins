"""When a briefing is due — in the operator's own time zone, across daylight-saving changes.

Slots are wall-clock times (Monday 08:30 Europe/Paris stays 08:30 on both sides of a DST
change). A cron job ticks ``navig email brief run --scheduled`` often; this module answers
"is a slot due that has not been served?" and, for a thin week, "postpone it?".

Config (``mailroom.brief`` in the space's ``.navig/config.yaml``), all optional::

    frequency: weekly        # daily | every_2_days | every_3_days | weekly | every_2_weeks |
                             # monthly | adaptive | manual
    weekday: monday
    time: "08:30"
    timezone: Europe/Paris
    max_sources: 6
    min_items: 3
    max_postpone_days: 14
    quiet_hours: "22:00-08:00"
    alert_days: 3            # events this close are announced on their own, not held for the briefing
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

FREQUENCIES = ("daily", "every_2_days", "every_3_days", "weekly", "every_2_weeks", "monthly", "adaptive", "manual")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


@dataclass
class BriefConfig:
    frequency: str = "weekly"
    weekday: int = 0
    at: time = time(8, 30)
    timezone: str = "Europe/Paris"
    max_sources: int = 6
    min_items: int = 3
    max_postpone_days: int = 14
    quiet_start: time = time(22, 0)
    quiet_end: time = time(8, 0)
    alert_days: int = 3
    raw: dict[str, Any] | None = None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None) -> BriefConfig:
        c = dict(cfg or {})
        freq = str(c.get("frequency") or "weekly").lower().replace("-", "_").replace(" ", "_")
        if freq not in FREQUENCIES:
            raise ValueError(f"brief frequency {freq!r} — one of {', '.join(FREQUENCIES)}")
        wd = str(c.get("weekday") or "monday").lower()
        if wd not in _WEEKDAYS:
            raise ValueError(f"brief weekday {wd!r}")
        q = str(c.get("quiet_hours") or "22:00-08:00")
        qs, _, qe = q.partition("-")
        out = cls(
            frequency=freq, weekday=_WEEKDAYS.index(wd), at=_hhmm(str(c.get("time") or "08:30")),
            timezone=str(c.get("timezone") or "Europe/Paris"), max_sources=int(c.get("max_sources") or 6),
            min_items=int(c.get("min_items") if c.get("min_items") is not None else 3),
            max_postpone_days=int(c.get("max_postpone_days") or 14),
            quiet_start=_hhmm(qs or "22:00"), quiet_end=_hhmm(qe or "08:00"),
            alert_days=int(c.get("alert_days") if c.get("alert_days") is not None else 3), raw=c,
        )
        ZoneInfo(out.timezone)  # raises on a typo now, not at 08:30 on Monday
        return out


def _hhmm(text: str) -> time:
    h, _, m = text.strip().partition(":")
    return time(int(h), int(m or 0))


def in_quiet_hours(now: datetime, cfg: BriefConfig) -> bool:
    t = now.astimezone(cfg.tz).time()
    if cfg.quiet_start <= cfg.quiet_end:
        return cfg.quiet_start <= t < cfg.quiet_end
    return t >= cfg.quiet_start or t < cfg.quiet_end


def _at(day: date, cfg: BriefConfig) -> datetime:
    return datetime.combine(day, cfg.at, tzinfo=cfg.tz)


def last_slot(now: datetime, cfg: BriefConfig, last_delivered: datetime | None) -> datetime | None:
    """The most recent scheduled moment at or before *now* (aware, local tz)."""
    local = now.astimezone(cfg.tz)
    today = local.date()
    f = cfg.frequency
    if f == "manual":
        return None
    if f in ("daily", "every_2_days", "every_3_days"):
        step = {"daily": 1, "every_2_days": 2, "every_3_days": 3}[f]
        slot = _at(today, cfg)
        if slot > local:
            slot = _at(today - timedelta(days=1), cfg)
        if last_delivered is not None and step > 1:
            nxt = _at(last_delivered.astimezone(cfg.tz).date() + timedelta(days=step), cfg)
            return nxt if nxt <= local else None
        return slot
    if f in ("weekly", "every_2_weeks", "adaptive"):
        back = (today.weekday() - cfg.weekday) % 7
        slot = _at(today - timedelta(days=back), cfg)
        if slot > local:
            slot = _at(today - timedelta(days=back + 7), cfg)
        if f == "every_2_weeks" and last_delivered is not None:
            if (slot.date() - last_delivered.astimezone(cfg.tz).date()).days < 14:
                return None
        return slot
    if f == "monthly":
        slot = _at(today.replace(day=1), cfg)
        if slot > local:
            prev = today.replace(day=1) - timedelta(days=1)
            slot = _at(prev.replace(day=1), cfg)
        return slot
    return None


@dataclass
class Due:
    due: bool
    slot: datetime | None = None
    reason: str = ""


def due(now: datetime, cfg: BriefConfig, *, last_delivered: datetime | None, last_slot_served: datetime | None) -> Due:
    if in_quiet_hours(now, cfg):
        return Due(False, reason="quiet hours")
    slot = last_slot(now, cfg, last_delivered)
    if slot is None:
        return Due(False, reason="manual only" if cfg.frequency == "manual" else "not due yet")
    if last_slot_served is not None and last_slot_served >= slot:
        return Due(False, slot, "this slot was already served")
    if last_delivered is not None and last_delivered >= slot:
        return Due(False, slot, "delivered since the slot")
    return Due(True, slot, f"slot {slot:%a %d %b %H:%M %Z}")


def should_postpone(item_count: int, now: datetime, cfg: BriefConfig, last_delivered: datetime | None) -> bool:
    """Thin briefing: wait for more, but never longer than ``max_postpone_days`` since the last one."""
    if item_count >= cfg.min_items:
        return False
    if last_delivered is None:
        return item_count == 0
    return (now - last_delivered) < timedelta(days=cfg.max_postpone_days)


def adaptive_due(item_count: int, now: datetime, cfg: BriefConfig, last_delivered: datetime | None) -> bool:
    """``adaptive``: deliver as soon as there is enough (and two days have passed), or at the cap."""
    if last_delivered is None:
        return item_count >= cfg.min_items
    gap = now - last_delivered
    return (item_count >= cfg.min_items and gap >= timedelta(days=2)) or (
        gap >= timedelta(days=cfg.max_postpone_days) and item_count > 0)
