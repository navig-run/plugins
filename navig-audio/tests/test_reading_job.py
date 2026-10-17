"""The reading job: background, one per chat, never a surprise bill.

What this layer owns that the engine deliberately does not (see ``reading_job``):

* **it returns at once** — a chapter takes minutes; if ``begin`` waited for the audio, the
  tool call or message handler that started it would time out and the listener would hear
  nothing until the end;
* **one reading per chat**, refused rather than queued;
* **no surprise bills** — quote after the cache, refuse past the credits left, confirm a big
  one, forget an unconfirmed one after a while;
* **stop** works and nothing is generated or charged after it.

Audio tools are byte-level stand-ins (the engine's own tests cover real ffmpeg); the engine
and the clip cache are the real thing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from navig_audio import reader, reading_job
from navig_audio.reader import CONFIRM_CREDITS, ReadOptions
from navig_audio.reading_job import Outcome, ReadEvent

STORY = " ".join(
    f"Sentence number {n} of the story, and it goes on for a while so the chunks fill up." for n in range(1, 200)
)
OPTS = ReadOptions(lang="en", part_minutes=1.0, max_chunk_chars=400, voice="ABCDEFGHIJKLMNOPQRST")
TEXT = STORY[:1250]  # 4 chunks in 2 parts of 2 (see test_reader._small_plan)
KEY = "telegram:42"


@dataclass
class _Timed:
    audio: bytes
    request_id: str | None = None


class _Config:
    tts_voice_id = "DEFAULTVOICEID00000000"
    output_format = "mp3_44100_128"


class FakeGen:
    def __init__(self, fail_on: int | None = None, gate: asyncio.Event | None = None):
        self.config = _Config()
        self.fail_on = fail_on
        self.gate = gate
        self.calls = 0

    async def tts_with_timestamps(self, text, voice_id, **kw):
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_on == self.calls:
            raise RuntimeError("provider exploded")
        return _Timed(b"\x00" * 6400, request_id=f"r{self.calls}")

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    """Stand-in audio tools, an isolated cache, and no jobs/pending left between tests."""
    monkeypatch.setattr(reader.cache, "podcast_cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(reader, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(
        reader, "concat", lambda parts, dst, *, gap_s=0.0, **kw: Path(dst).write_bytes(
            b"".join(Path(p).read_bytes() for p in parts)
        )
    )
    monkeypatch.setattr(
        reader, "normalize", lambda src, dst, *, i, **kw: Path(dst).write_bytes(Path(src).read_bytes())
    )
    monkeypatch.setattr(reader, "probe_duration", lambda p, **kw: Path(p).stat().st_size / 8000.0)
    reading_job._JOBS.clear()
    reading_job._PENDING.clear()
    yield
    for job in list(reading_job._JOBS.values()):
        job.cancel.set()
    reading_job._JOBS.clear()
    reading_job._PENDING.clear()


class Harness:
    """Collects what the caller would see: delivered parts and events."""

    def __init__(self, deliver_ok: bool = True):
        self.delivered: list[int] = []
        self.events: list[ReadEvent] = []
        self.finished = asyncio.Event()
        self.deliver_ok = deliver_ok

    async def deliver(self, part):
        self.delivered.append(part.index)
        return self.deliver_ok

    async def notify(self, event: ReadEvent):
        self.events.append(event)
        if event.kind in {"done", "partial", "cancelled", "failed"}:
            self.finished.set()

    @property
    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]


async def start(h: Harness, *, gen=None, text=TEXT, options=OPTS, remaining=None, **kw):
    async def remaining_fn():
        return remaining

    return await reading_job.begin(
        KEY, text, options, deliver=h.deliver, notify=h.notify,
        remaining_fn=remaining_fn, generator=gen or FakeGen(), **kw,
    )


async def finish(h: Harness):
    await asyncio.wait_for(h.finished.wait(), timeout=10)


# ── starting ──────────────────────────────────────────────────────────────────


async def test_begin_returns_before_any_audio_exists():
    """THE property: the caller (a tool call, a message handler) must not wait for minutes."""
    h = Harness()
    gate = asyncio.Event()  # the provider is "slow": it will not answer until we say so
    gen = FakeGen(gate=gate)

    result = await asyncio.wait_for(start(h, gen=gen), timeout=2)

    assert result.outcome is Outcome.STARTED and result.plan.part_count == 2
    assert h.delivered == [] and h.events == [], "begin() returned only after the audio was made"
    assert reading_job.current(KEY) is not None
    gate.set()
    await finish(h)


async def test_a_reading_delivers_every_part_in_order_then_reports_done():
    h = Harness()
    result = await start(h)
    await finish(h)

    assert h.delivered == [1, 2] == [p for p in range(1, result.plan.part_count + 1)]
    assert h.kinds == ["part", "part", "done"]
    done = h.events[-1].summary
    assert done.complete and len(done.parts) == 2 and done.credits_spent == result.plan.chars
    assert reading_job.current(KEY) is None, "a finished reading must free the chat for the next"


async def test_part_events_carry_the_part_so_the_caller_can_caption_it():
    h = Harness()
    await start(h)
    await finish(h)
    parts = [e.part for e in h.events if e.kind == "part"]
    assert [p.index for p in parts] == [1, 2] and all(p.total == 2 for p in parts)


# ── one at a time ─────────────────────────────────────────────────────────────


async def test_a_second_reading_in_the_same_chat_is_refused_not_queued():
    h = Harness()
    gate = asyncio.Event()
    first = await start(h, gen=FakeGen(gate=gate))
    gen2 = FakeGen()
    second = await start(Harness(), gen=gen2, text=STORY[1300:2550])

    assert first.outcome is Outcome.STARTED
    assert second.outcome is Outcome.BUSY and second.plan is first.plan
    assert gen2.calls == 0, "a refused reading must not spend a credit"
    gate.set()
    await finish(h)

    again = await start(Harness(), text=STORY[1300:2550])
    assert again.outcome is Outcome.STARTED, "the chat must be free once the reading ends"


async def test_different_chats_read_independently():
    a, b = Harness(), Harness()
    gate = asyncio.Event()
    ra = await reading_job.begin("telegram:1", TEXT, OPTS, deliver=a.deliver, notify=a.notify,
                                 generator=FakeGen(gate=gate), remaining_fn=lambda: _none())
    rb = await reading_job.begin("telegram:2", STORY[1300:2550], OPTS, deliver=b.deliver,
                                 notify=b.notify, generator=FakeGen(gate=gate), remaining_fn=lambda: _none())
    assert ra.outcome is Outcome.STARTED and rb.outcome is Outcome.STARTED
    gate.set()
    await finish(a)
    await finish(b)


async def _none():
    return None


# ── no surprise bills ─────────────────────────────────────────────────────────


async def test_nothing_readable_is_refused_with_a_reason():
    r = await start(Harness(), text="   ")
    assert r.outcome is Outcome.EMPTY and "no readable text" in r.detail


async def test_a_reading_the_account_cannot_afford_is_refused_before_it_starts():
    gen = FakeGen()
    r = await start(Harness(), gen=gen, remaining=100)
    assert r.outcome is Outcome.INSUFFICIENT and r.remaining == 100 and r.spend > 100
    assert gen.calls == 0 and reading_job.current(KEY) is None


async def test_confirming_does_not_bypass_the_credit_check():
    """`/read go` is consent to a COST, not a way to overdraw the account."""
    r = await start(Harness(), remaining=100, confirmed=True)
    assert r.outcome is Outcome.INSUFFICIENT


async def test_a_big_reading_waits_for_a_yes_and_remembers_what_it_was():
    h = Harness()
    gen = FakeGen()
    r = await start(h, gen=gen, confirm_credits=500)  # TEXT is ~1.1k credits
    assert r.outcome is Outcome.NEEDS_CONFIRM and r.spend > 500
    assert gen.calls == 0 and reading_job.current(KEY) is None
    assert reading_job.pending_plan(KEY).chars == r.plan.chars

    go = await reading_job.confirm(
        KEY, deliver=h.deliver, notify=h.notify, generator=FakeGen(), confirm_credits=500,
        remaining_fn=lambda: _none(),
    )
    assert go.outcome is Outcome.STARTED
    await finish(h)
    assert h.delivered == [1, 2]
    assert reading_job.pending_plan(KEY) is None, "a confirmed reading must not stay pending"


async def test_confirm_with_nothing_pending_says_so():
    r = await reading_job.confirm(KEY, deliver=Harness().deliver, notify=Harness().notify)
    assert r.outcome is Outcome.NOTHING_PENDING


async def test_an_unconfirmed_reading_expires(monkeypatch):
    """"go" tomorrow morning must not start last night's book."""
    await start(Harness(), confirm_credits=500)
    assert reading_job.pending_plan(KEY) is not None
    monkeypatch.setattr(reading_job, "PENDING_TTL_S", -1)
    assert reading_job.pending_plan(KEY) is None
    r = await reading_job.confirm(KEY, deliver=Harness().deliver, notify=Harness().notify)
    assert r.outcome is Outcome.NOTHING_PENDING


async def test_a_pending_reading_can_be_cancelled():
    await start(Harness(), confirm_credits=500)
    assert reading_job.discard_pending(KEY) is True
    assert reading_job.discard_pending(KEY) is False
    assert reading_job.pending_plan(KEY) is None


async def test_the_quote_is_after_the_cache_so_a_resend_is_free():
    """Re-sending a chapter already read must not ask for a yes to spend 0 credits."""
    h = Harness()
    first = await start(h)
    await finish(h)
    assert first.spend > 0

    h2 = Harness()
    again = await start(h2, confirm_credits=0)  # any real spend would need a confirmation
    assert again.spend == 0, "the cache was not consulted when quoting"
    assert again.outcome is Outcome.STARTED, "a free re-read was held up for confirmation"
    await finish(h2)
    assert h2.events[-1].summary.credits_spent == 0


async def test_the_default_confirmation_threshold_is_the_documented_one():
    assert CONFIRM_CREDITS == 15_000
    big = " ".join(f"Sentence number {n} of a very long story, told at length." for n in range(1, 900))
    r = await start(Harness(), text=big)
    assert r.plan.credits > CONFIRM_CREDITS
    assert r.outcome is Outcome.NEEDS_CONFIRM


async def test_remaining_credits_unknown_never_blocks_but_never_counts_as_plenty(monkeypatch):
    from navig_audio.podcast import voices

    async def broken():
        raise ConnectionError("offline")

    monkeypatch.setattr(voices, "account", broken)
    assert await reading_job._remaining_credits() is None

    async def sub():
        return {"character_limit": 100_000, "character_count": 61_500}

    monkeypatch.setattr(voices, "account", sub)
    assert await reading_job._remaining_credits() == 38_500


# ── stopping ──────────────────────────────────────────────────────────────────


async def test_stop_halts_the_reading_and_nothing_more_is_made():
    """The listener says stop after the first part arrives: part 2 must never be generated
    (it would be charged for audio nobody will hear)."""
    h = Harness()
    gen = FakeGen()

    async def deliver_then_stop(part):
        h.delivered.append(part.index)
        assert reading_job.stop(KEY) is True
        return True

    started = await reading_job.begin(
        KEY, TEXT, OPTS, deliver=deliver_then_stop, notify=h.notify, generator=gen,
        remaining_fn=lambda: _none(),
    )
    assert started.outcome is Outcome.STARTED
    await finish(h)

    assert h.kinds[-1] == "cancelled" and h.delivered == [1]
    assert gen.calls == 2, "only part 1's two chunks may be generated; nothing after a stop"
    assert reading_job.current(KEY) is None


async def test_stop_with_nothing_running_is_false():
    assert reading_job.stop(KEY) is False


# ── failure ───────────────────────────────────────────────────────────────────


async def test_an_error_after_some_parts_is_reported_as_partial_with_what_exists():
    h = Harness()
    await start(h, gen=FakeGen(fail_on=3))  # first chunk of part 2
    await finish(h)
    assert h.kinds == ["part", "partial"]
    assert h.events[-1].summary.parts and "provider exploded" in h.events[-1].detail


async def test_an_error_before_any_audio_is_reported_as_failed():
    h = Harness()
    await start(h, gen=FakeGen(fail_on=1))
    await finish(h)
    assert h.kinds == ["failed"] and "provider exploded" in h.events[-1].detail
    assert reading_job.current(KEY) is None


async def test_a_notification_that_raises_does_not_stop_the_reading():
    h = Harness()

    async def bad_notify(event):
        raise ConnectionError("the listener blocked the bot")

    r = await reading_job.begin(
        KEY, TEXT, OPTS, deliver=h.deliver, notify=bad_notify, generator=FakeGen(),
        remaining_fn=lambda: _none(),
    )
    assert r.outcome is Outcome.STARTED
    for _ in range(100):
        await asyncio.sleep(0.05)
        if reading_job.current(KEY) is None:
            break
    assert h.delivered == [1, 2], "a broken notification must not cost the listener their book"
