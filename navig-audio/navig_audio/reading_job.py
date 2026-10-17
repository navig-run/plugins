"""Run a reading in the background, one per chat — start it, stop it, be told how it went.

A chapter takes minutes to synthesise, so a reading cannot live inside a single tool call or
a single message handler: those time out, and the listener would hear nothing until the
very end. :func:`begin` starts the reading as a background task and returns at once; each
part is handed to ``deliver`` as it lands (see :mod:`navig_audio.reader`).

This layer owns the *policy* the engine deliberately does not:

* **one reading per chat** — a second ``/read`` while one is running is refused, not queued
  (two voices reading at once, or a surprise second bill, is the worse surprise);
* **no surprise bills** — the quote is cache-aware (:func:`~navig_audio.reader.estimate_spend`),
  a reading that exceeds the credits left on the account is refused outright, and one above
  :data:`~navig_audio.reader.CONFIRM_CREDITS` waits for an explicit yes;
* **stop** — cancels between chunks, so nothing is generated (or charged) after it.

It speaks in structured :class:`ReadEvent` s, never in sentences: the caller knows the
listener's language and surface and phrases them. Standalone-friendly — no import of core.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from navig_audio import reader
from navig_audio.reader import (
    CONFIRM_CREDITS,
    ReadError,
    ReadOptions,
    ReadPart,
    ReadPlan,
    ReadSummary,
)

__all__ = [
    "Outcome",
    "ReadEvent",
    "StartResult",
    "begin",
    "confirm",
    "current",
    "discard_pending",
    "pending_plan",
    "stop",
]

_log = logging.getLogger(__name__)

#: How long an unconfirmed big reading is remembered. Long enough to type "go" half asleep,
#: short enough that "go" tomorrow does not start yesterday's book.
PENDING_TTL_S = 30 * 60


class Outcome(str, Enum):
    STARTED = "started"
    NEEDS_CONFIRM = "needs_confirm"
    BUSY = "busy"
    EMPTY = "empty"
    INSUFFICIENT = "insufficient"
    NOTHING_PENDING = "nothing_pending"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class StartResult:
    outcome: Outcome
    plan: ReadPlan | None = None
    #: Credits this reading would charge now (after the cache) — what the quote is about.
    spend: int = 0
    #: Credits left on the account, when the provider would say.
    remaining: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class ReadEvent:
    """Something the listener should be told. ``kind`` is one of: ``part`` (delivered),
    ``done``, ``partial`` (stopped by an error), ``cancelled``, ``failed`` (nothing made)."""

    kind: str
    summary: ReadSummary | None = None
    part: ReadPart | None = None
    detail: str = ""


Deliver = Callable[[ReadPart], Awaitable[bool]]
Notify = Callable[[ReadEvent], Awaitable[None]]


@dataclass
class _Job:
    key: str
    plan: ReadPlan
    cancel: asyncio.Event
    started: float = field(default_factory=time.monotonic)
    delivered: int = 0
    task: asyncio.Task[Any] | None = None


@dataclass
class _Pending:
    text: str
    options: ReadOptions
    created: float = field(default_factory=time.monotonic)


_JOBS: dict[str, _Job] = {}
_PENDING: dict[str, _Pending] = {}
_TASKS: set[asyncio.Task[Any]] = set()  # strong refs: a bare task can be collected mid-flight


def current(key: str) -> _Job | None:
    """The running job for *key*, if any."""
    job = _JOBS.get(key)
    if job is not None and job.task is not None and job.task.done():
        _JOBS.pop(key, None)
        return None
    return job


def pending_plan(key: str) -> ReadPlan | None:
    """The plan waiting for a yes on *key*, unless it has expired."""
    pending = _PENDING.get(key)
    if pending is None:
        return None
    if time.monotonic() - pending.created > PENDING_TTL_S:
        _PENDING.pop(key, None)
        return None
    try:
        return reader.plan_reading(pending.text, pending.options)
    except ReadError:
        return None


def discard_pending(key: str) -> bool:
    return _PENDING.pop(key, None) is not None


def stop(key: str) -> bool:
    """Stop the reading on *key* after the chunk in flight. True if one was running."""
    job = current(key)
    if job is None:
        return False
    job.cancel.set()
    return True


async def _remaining_credits() -> int | None:
    """Credits left on the account, or ``None`` when the provider will not say.

    ``None`` is never read as "plenty": it only means the guard cannot run, and the
    estimate and confirmation threshold still apply.
    """
    try:
        from navig_audio.podcast.voices import account

        sub = await account()
        limit, used = sub.get("character_limit"), sub.get("character_count")
        if isinstance(limit, (int, float)) and isinstance(used, (int, float)):
            return max(int(limit - used), 0)
    except Exception:  # noqa: BLE001 — no key / offline: the other guards still apply
        return None
    return None


def _cheap_voice_id(options: ReadOptions) -> str | None:
    """A voice id knowable without a network call, so the cache can be consulted."""
    from navig_generate.tools.audio_generation import AudioGenerationConfig

    if not options.voice:
        return str(AudioGenerationConfig.from_env().tts_voice_id)
    return options.voice if reader._VOICE_ID.match(options.voice) else None


async def begin(
    key: str,
    text: str,
    options: ReadOptions,
    *,
    deliver: Deliver,
    notify: Notify,
    confirmed: bool = False,
    confirm_credits: int = CONFIRM_CREDITS,
    out_root: Path | None = None,
    remaining_fn: Callable[[], Awaitable[int | None]] | None = None,
    generator: Any = None,
) -> StartResult:
    """Plan a reading and, if it is safe to, start it in the background.

    Returns immediately. ``confirmed`` skips only the *cost confirmation*; running out of
    credits and an already-running reading are refused regardless.
    """
    running = current(key)
    if running is not None:
        return StartResult(Outcome.BUSY, plan=running.plan)
    try:
        plan = reader.plan_reading(text, options)
    except ReadError as exc:
        return StartResult(Outcome.EMPTY, detail=str(exc))

    spend = reader.estimate_spend(plan, options, _cheap_voice_id(options))
    remaining = await (remaining_fn or _remaining_credits)()
    if remaining is not None and spend > remaining:
        return StartResult(Outcome.INSUFFICIENT, plan=plan, spend=spend, remaining=remaining)
    if spend > confirm_credits and not confirmed:
        _PENDING[key] = _Pending(text=text, options=options)
        return StartResult(Outcome.NEEDS_CONFIRM, plan=plan, spend=spend, remaining=remaining)

    _PENDING.pop(key, None)
    job = _Job(key=key, plan=plan, cancel=asyncio.Event())
    _JOBS[key] = job
    out_dir = (out_root or _default_out_root()) / plan.slug
    job.task = asyncio.ensure_future(
        _run(job, options, out_dir, deliver, notify, generator)
    )
    _TASKS.add(job.task)
    job.task.add_done_callback(_TASKS.discard)
    return StartResult(Outcome.STARTED, plan=plan, spend=spend, remaining=remaining)


async def confirm(key: str, options: ReadOptions | None = None, **kwargs: Any) -> StartResult:
    """Start the reading that was waiting for a yes on *key*."""
    pending = _PENDING.get(key)
    if pending is None or time.monotonic() - pending.created > PENDING_TTL_S:
        _PENDING.pop(key, None)
        return StartResult(Outcome.NOTHING_PENDING)
    return await begin(
        key, pending.text, options or pending.options, confirmed=True, **kwargs
    )


def _default_out_root() -> Path:
    from navig_sdk.host import cache_dir

    return cache_dir() / "reading"


async def _run(
    job: _Job,
    options: ReadOptions,
    out_dir: Path,
    deliver: Deliver,
    notify: Notify,
    generator: Any,
) -> None:
    async def tracked_deliver(part: ReadPart) -> bool:
        ok = await deliver(part)
        if ok:
            job.delivered += 1
            await _safe_notify(notify, ReadEvent("part", part=part))
        return ok

    summary: ReadSummary | None = None
    try:
        summary = await reader.read_aloud(
            job.plan,
            options,
            out_dir,
            deliver=tracked_deliver,
            cancel=job.cancel,
            generator=generator,
        )
    except ReadError as exc:  # ffmpeg missing: nothing began
        await _safe_notify(notify, ReadEvent("failed", detail=str(exc)))
        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — a background task must say how it died
        await _safe_notify(notify, ReadEvent("failed", detail=f"{type(exc).__name__}: {exc}"))
        return
    finally:
        _JOBS.pop(job.key, None)

    if summary.cancelled:
        kind = "cancelled"
    elif summary.error and not summary.parts:
        kind = "failed"
    elif summary.error:
        kind = "partial"
    else:
        kind = "done"
    await _safe_notify(notify, ReadEvent(kind, summary=summary, detail=summary.error or ""))


async def _safe_notify(notify: Notify, event: ReadEvent) -> None:
    """A notification must never take the reading down with it."""
    try:
        await notify(event)
    except Exception:  # noqa: BLE001 — the listener may have blocked the bot; keep reading
        _log.debug("notify(%s) failed; the reading continues", event.kind, exc_info=True)
