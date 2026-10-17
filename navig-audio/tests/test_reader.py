"""The bedtime reader's engine: a text in, numbered audio parts out, delivered as they land.

Real ffmpeg (joining, loudness, duration) against a FAKE provider — so the audio plumbing is
exercised for real and no credits are spent. What each group pins, and why it is its own test:

* **text preparation** keeps every word (the words are the user's) and only removes what a
  voice would stumble on;
* **language detection** returns ``None`` rather than guess — a wrong ``language_code``
  mispronounces a whole chapter, no code lets the model decide;
* **the plan** carries the exact cost before anything is spent;
* **delivery ordering** — part 1 reaches the listener while later parts are still being made.
  That ordering is the whole point of the feature;
* **the cache** — a retry or a re-send re-buys nothing;
* **partial failure** — a provider error on chunk 5 must not discard parts 1-4, which exist
  and have already been delivered.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from navig_media_core.media.audio_edit import ffmpeg_available

from navig_audio import reader
from navig_audio.reader import (
    ReadError,
    ReadOptions,
    clean_text,
    detect_language,
    options_from_config,
    plan_reading,
    read_aloud,
)



# ── text preparation ──────────────────────────────────────────────────────────


def test_clean_text_removes_marks_but_keeps_every_word():
    raw = (
        "# Chapter One\n\nIt was a **dark** and _stormy_ night — see [the map](http://x.io/m) "
        "or http://spam.example/x now 😀.\n\n- first\n- second\n\n```\ncode()\n```\n\nThe end"
    )
    out = clean_text(raw)
    assert "#" not in out and "**" not in out and "http" not in out and "😀" not in out
    assert "code()" not in out, "a code block is not read aloud"
    for word in ("Chapter", "One", "dark", "stormy", "night", "the", "map", "first", "second", "end"):
        assert word in out, f"clean_text dropped the word {word!r}"


def test_clean_text_ends_paragraphs_and_headings_with_a_stop():
    """A heading with no punctuation is otherwise glued onto the next sentence."""
    out = clean_text("# Prologue\n\nOnce upon a time\n\nThe end.")
    assert out.split("\n\n") == ["Prologue.", "Once upon a time.", "The end."]


def test_clean_text_does_not_double_a_stop_or_touch_quotes():
    assert clean_text("Really?") == "Really?"
    assert clean_text("Il dit « oui »") == "Il dit « oui »"
    assert clean_text("Он сказал: «да».") == "Он сказал: «да»."


def test_clean_text_of_nothing_is_empty():
    assert clean_text("") == "" and clean_text("   \n\n  ") == "" and clean_text("```x```") == ""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Жили-были старик со старухой у самого синего моря. И не было у них ничего.", "ru"),
        ("Il était une fois un roi qui n'avait pas de fils, et il le regrettait dans son cœur.", "fr"),
        ("Once upon a time there was a king, and he was the best of all the kings in the land.", "en"),
        ("Había una vez un rey que no tenía hijos y lo lamentaba mucho en su corazón.", None),
        ("ok", None),
        ("1234 5678", None),
        ("", None),
    ],
)
def test_detect_language_guesses_only_when_it_can(text, expected):
    assert detect_language(text) == expected


# ── the plan ──────────────────────────────────────────────────────────────────

STORY = " ".join(
    f"Sentence number {n} of the story, and it goes on for a while so the chunks fill up." for n in range(1, 200)
)


def test_the_plan_knows_its_cost_before_anything_is_spent():
    plan = plan_reading(STORY, ReadOptions(lang="en"))
    assert plan.chars == sum(len(c.text) for c in plan.chunks)
    assert plan.credits == plan.chars, "multilingual_v2 bills one credit per character"
    assert plan.minutes == pytest.approx(plan.chars / (950 * 0.9))
    assert plan.part_count == len(plan.parts) >= 1
    assert plan.lang == "en"
    d = plan.to_dict()
    assert d["credits"] == plan.credits and d["parts"] == plan.part_count


def test_chunks_respect_the_limit_and_carry_context():
    plan = plan_reading(STORY, ReadOptions(max_chunk_chars=1000))
    assert len(plan.chunks) > 3
    assert all(c.chars <= 1000 for c in plan.chunks)
    assert plan.chunks[0].previous_text is None and plan.chunks[-1].next_text is None
    for before, after in zip(plan.chunks, plan.chunks[1:]):
        assert after.previous_text and after.previous_text in before.text, "context must be the neighbour's tail"
        assert before.next_text and after.text.startswith(before.next_text[:40])


def test_parts_cover_every_chunk_exactly_once_in_order():
    plan = plan_reading(STORY, ReadOptions(part_minutes=2.0, max_chunk_chars=1500))
    assert plan.part_count >= 2
    flat = [c.index for part in plan.parts for c in part]
    assert flat == [c.index for c in plan.chunks] == list(range(len(plan.chunks)))
    budget = int(2.0 * 950 * 0.9)
    for part in plan.parts[:-1]:  # a part may exceed only by being a single oversized chunk
        assert sum(c.chars for c in part) <= budget or len(part) == 1


def test_a_slower_pace_lengthens_the_estimate():
    fast = plan_reading(STORY, ReadOptions(speed=1.1))
    slow = plan_reading(STORY, ReadOptions(speed=0.8))
    assert slow.minutes > fast.minutes and slow.credits == fast.credits


def test_the_slug_is_stable_per_text():
    assert plan_reading(STORY).slug == plan_reading(STORY).slug
    assert plan_reading(STORY).slug != plan_reading(STORY + " more").slug


def test_nothing_readable_is_refused_with_a_way_forward():
    with pytest.raises(ReadError, match="no readable text"):
        plan_reading("   ")


# ── options ───────────────────────────────────────────────────────────────────


def test_overrides_win_and_none_means_unset():
    o = options_from_config(voice="Marcus", speed=0.85, lang=None, part_minutes=None)
    assert o.voice == "Marcus" and o.speed == 0.85
    assert o.lang is None and o.part_minutes == reader.DEFAULT_PART_MINUTES


def test_speed_is_clamped_to_the_providers_range():
    assert options_from_config(speed=0.1).speed == 0.7
    assert options_from_config(speed=9).speed == 1.2


def test_string_numbers_from_config_are_accepted_and_garbage_is_named():
    """`navig config set audio.read.speed 0.85` stores the STRING "0.85"."""
    assert options_from_config(speed="0.85").speed == 0.85
    with pytest.raises(ReadError, match="not a number"):
        options_from_config(speed="slow")


# ── synthesis (real ffmpeg, fake provider) ────────────────────────────────────


@pytest.fixture(scope="module")
def _real_tone(tmp_path_factory) -> bytes:
    """A valid 0.8 s mp3 — real bytes for ffmpeg to join, so concat/normalize/probe are real.
    Skips (never fails) on a machine without ffmpeg: only the real-audio tests need it."""
    if not ffmpeg_available():
        pytest.skip("ffmpeg is not on PATH")
    path = tmp_path_factory.mktemp("fx") / "tone.mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=330:duration=0.8",
         "-acodec", "libmp3lame", "-b:a", "64k", str(path)],
        check=True,
    )
    return path.read_bytes()


@pytest.fixture
def tone(request) -> bytes:
    """The audio the fake provider returns: a REAL mp3 when the test is about audio
    (``real_audio``), otherwise 6.4 KB of filler — the fast stand-ins only count bytes."""
    if "real_audio" in request.fixturenames:
        return request.getfixturevalue("_real_tone")
    return b"\x00" * 6400


@dataclass
class _Timed:
    audio: bytes
    request_id: str | None = None


class _Config:
    tts_voice_id = "DEFAULTVOICEID00000000"
    output_format = "mp3_44100_128"


class FakeGen:
    """Stands in for ElevenLabs. Records the order of generation and delivery."""

    def __init__(self, audio: bytes, events: list[str], fail_on: int | None = None):
        self.config = _Config()
        self.audio = audio
        self.events = events
        self.fail_on = fail_on
        self.calls: list[dict] = []
        self.closed = False

    async def tts_with_timestamps(self, text, voice_id, **kw):
        n = len(self.calls) + 1
        self.calls.append({"text": text, "voice_id": voice_id, **kw})
        self.events.append(f"gen{n}")
        if self.fail_on == n:
            raise RuntimeError("provider exploded")
        return _Timed(self.audio, request_id=f"req{n}")

    async def close(self):
        self.closed = True


@pytest.fixture
def real_audio():
    """Opt-in marker: a test that requests this runs the REAL loudness normaliser."""
    return None


@pytest.fixture(autouse=True)
def _fast_audio(request, monkeypatch):
    """Most of these tests are about ordering, caching and failure, not audio — and every
    real ``concat`` / ``normalize`` / ``probe_duration`` is an ffmpeg process (~0.5-2 s on a
    loaded Windows box, ~15 per test). They get byte-level stand-ins whose durations still
    scale with the clips (size / 8000 ≈ the real 0.8 s tone); tests that ARE about the audio
    request ``real_audio`` and run the real tools."""
    if "real_audio" in request.fixturenames:
        return

    def fake_concat(parts, dst, *, gap_s=0.0, **kw):
        Path(dst).write_bytes(b"".join(Path(p).read_bytes() for p in parts))

    monkeypatch.setattr(reader, "ffmpeg_available", lambda: True)  # the stand-ins need none
    monkeypatch.setattr(reader, "concat", fake_concat)
    monkeypatch.setattr(
        reader, "normalize", lambda src, dst, *, i, **kw: Path(dst).write_bytes(Path(src).read_bytes())
    )
    monkeypatch.setattr(reader, "probe_duration", lambda p, **kw: Path(p).stat().st_size / 8000.0)


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """The clip cache is shared with the podcast renderer — never touch the real one."""
    root = tmp_path / "cache"
    monkeypatch.setattr(reader.cache, "podcast_cache_dir", lambda: root)
    return root


MULTI = ReadOptions(lang="en", part_minutes=1.0, max_chunk_chars=400, voice="ABCDEFGHIJKLMNOPQRST")


def _multi_plan():
    """~6 chunks in 3 parts: enough to prove ordering, chaining and partial failure."""
    plan = plan_reading(STORY[:2400], MULTI)
    assert plan.part_count >= 3 and len(plan.chunks) >= 6, "fixture must span several parts"
    assert max(len(p) for p in plan.parts) >= 2, "a part must hold several chunks to test joining"
    return plan


def _small_plan():
    """2 parts of 2 chunks — the minimum that still exercises real joining, for the tests
    that run the real ffmpeg tools (each part costs several process spawns)."""
    plan = plan_reading(STORY[:1250], MULTI)  # 1100-1350 all give 4 chunks / 2 parts of 2
    assert plan.part_count == 2 and all(len(p) == 2 for p in plan.parts), plan.to_dict()
    return plan


async def test_parts_are_delivered_before_the_next_part_is_generated(tone, tmp_path, isolated_cache):
    """THE feature: the listener hears part 1 while part 2 is still being made."""
    events: list[str] = []
    gen = FakeGen(tone, events)
    plan = _multi_plan()

    async def deliver(part):
        events.append(f"deliver{part.index}")
        return True

    summary = await read_aloud(plan, MULTI, tmp_path / "out", deliver=deliver, generator=gen)

    assert summary.complete and len(summary.parts) == plan.part_count
    first_delivery = events.index("deliver1")
    last_chunk_of_part1 = f"gen{len(plan.parts[0])}"
    assert events.index(last_chunk_of_part1) < first_delivery, "part 1 delivered before it was built"
    assert any(e.startswith("gen") for e in events[first_delivery + 1 :]), (
        "part 1 was delivered only AFTER every chunk was generated — that is a batch job, "
        "not a reader (the first words must arrive while the rest is still being made)"
    )
    deliveries = [e for e in events if e.startswith("deliver")]
    assert deliveries == [f"deliver{i}" for i in range(1, plan.part_count + 1)], "parts must arrive in order"


async def test_the_audio_is_real_numbered_and_measured(
    tone, tmp_path, isolated_cache, monkeypatch, real_audio
):
    """The one test that runs every real ffmpeg step: join, loudness, duration probe."""
    targets: list[float] = []
    real_normalize = reader.normalize

    def spy(src, dst, *, i, **kw):
        targets.append(i)
        return real_normalize(src, dst, i=i, **kw)

    monkeypatch.setattr(reader, "normalize", spy)
    gen = FakeGen(tone, [])
    plan = _small_plan()
    summary = await read_aloud(plan, MULTI, tmp_path / "out", generator=gen)
    assert targets and set(targets) == {-20.0}, (
        "bedtime audio must sit well below the -16 LUFS podcast level"
    )

    names = [p.path.name for p in summary.parts]
    assert names == [f"{plan.slug}-part-{i:02d}-of-{plan.part_count:02d}.mp3" for i in range(1, plan.part_count + 1)]
    for part, group in zip(summary.parts, plan.parts):
        assert part.path.exists() and part.path.stat().st_size > 0
        assert part.duration_s > len(group) * 0.5, "duration must be the real, probed length"
        assert part.chars == sum(c.chars for c in group)
    assert not list((tmp_path / "out").glob(".*joined*")), "scratch files must not be left behind"
    assert summary.parts[0].caption.startswith(f"1/{plan.part_count} · 0:")


async def test_a_second_run_buys_nothing(tone, tmp_path, isolated_cache):
    """A retry, a re-send or a resumed job must cost zero credits."""
    plan = _multi_plan()
    first = FakeGen(tone, [])
    s1 = await read_aloud(plan, MULTI, tmp_path / "a", generator=first)
    assert s1.generated_chunks == len(plan.chunks) and s1.credits_spent == plan.chars

    second = FakeGen(tone, [])
    s2 = await read_aloud(plan, MULTI, tmp_path / "b", generator=second)
    assert second.calls == [], "the cache was not used — a re-run re-bought the text"
    assert s2.cached_chunks == len(plan.chunks) and s2.credits_spent == 0 and s2.complete


async def test_a_changed_voice_is_not_served_from_the_other_voices_cache(tone, tmp_path, isolated_cache):
    plan = _multi_plan()
    await read_aloud(plan, MULTI, tmp_path / "a", generator=FakeGen(tone, []))
    other = ReadOptions(**{**MULTI.__dict__, "voice": "ZYXWVUTSRQPONMLKJIHG"})
    gen = FakeGen(tone, [])
    s = await read_aloud(plan, other, tmp_path / "b", generator=gen)
    assert len(gen.calls) == len(plan.chunks) and s.cached_chunks == 0


async def test_a_provider_failure_keeps_the_parts_already_made(tone, tmp_path, isolated_cache):
    """Chunk N explodes: parts before it exist AND were delivered. Discarding them would
    throw away audio someone has already heard."""
    plan = _multi_plan()
    fail_at = len(plan.parts[0]) + 1  # first chunk of part 2
    gen = FakeGen(tone, [], fail_on=fail_at)
    delivered: list[int] = []

    async def deliver(part):
        delivered.append(part.index)
        return True

    s = await read_aloud(plan, MULTI, tmp_path / "out", deliver=deliver, generator=gen)

    assert not s.complete and "provider exploded" in (s.error or "")
    assert [p.index for p in s.parts] == [1] and delivered == [1]
    assert s.parts[0].path.exists()


async def test_a_failed_delivery_is_counted_and_reading_continues(tone, tmp_path, isolated_cache):
    plan = _multi_plan()

    async def deliver(part):
        return part.index != 2  # the network drops part 2

    s = await read_aloud(plan, MULTI, tmp_path / "out", deliver=deliver, generator=FakeGen(tone, []))
    assert s.delivery_failures == 1 and len(s.parts) == plan.part_count
    assert s.complete, "one lost part must not abort the rest of the book"


async def test_a_delivery_that_raises_does_not_abort_reading(tone, tmp_path, isolated_cache):
    plan = _multi_plan()

    async def deliver(part):
        raise ConnectionError("telegram is down")

    s = await read_aloud(plan, MULTI, tmp_path / "out", deliver=deliver, generator=FakeGen(tone, []))
    assert s.delivery_failures == plan.part_count and len(s.parts) == plan.part_count


async def test_cancelling_stops_between_chunks(tone, tmp_path, isolated_cache):
    plan = _multi_plan()
    cancel = asyncio.Event()
    gen = FakeGen(tone, [])

    async def deliver(part):
        cancel.set()  # the listener says stop after the first part arrives
        return True

    s = await read_aloud(plan, MULTI, tmp_path / "out", deliver=deliver, cancel=cancel, generator=gen)
    assert s.cancelled and len(s.parts) == 1
    assert len(gen.calls) == len(plan.parts[0]), "nothing may be generated after stop"


async def test_chunks_chain_context_language_voice_and_pace(tone, tmp_path, isolated_cache):
    plan = _multi_plan()
    gen = FakeGen(tone, [])
    await read_aloud(plan, MULTI, tmp_path / "out", generator=gen)

    assert all(c["language_code"] == "en" for c in gen.calls)
    assert all(c["voice_id"] == "ABCDEFGHIJKLMNOPQRST" for c in gen.calls)
    assert all(c["voice_settings"]["speed"] == 0.9 for c in gen.calls), "bedtime pace must reach the provider"
    assert gen.calls[0]["previous_request_ids"] is None
    assert gen.calls[1]["previous_request_ids"] == ["req1"], "chunk 2 must continue chunk 1's delivery"
    assert gen.calls[3]["previous_request_ids"] == ["req1", "req2", "req3"]
    assert gen.calls[-1]["next_text"] is None and gen.calls[1]["previous_text"]


async def test_a_voice_is_resolved_by_name(tone, tmp_path, isolated_cache, monkeypatch):
    from navig_audio.podcast import voices

    async def fake_list(mine_only=False):
        return [voices.Voice("MARCUSVOICEID0000000", "Marcus", "cloned", {})]

    monkeypatch.setattr(voices, "list_voices", fake_list)
    opts = ReadOptions(**{**MULTI.__dict__, "voice": "marcus"})
    gen = FakeGen(tone, [])
    await read_aloud(plan_reading(STORY[:2500], opts), opts, tmp_path / "out", generator=gen)
    assert gen.calls and gen.calls[0]["voice_id"] == "MARCUSVOICEID0000000"


async def test_an_unknown_voice_is_reported_before_any_credit_is_spent(tone, tmp_path, isolated_cache, monkeypatch):
    from navig_audio.podcast import voices

    async def fake_list(mine_only=False):
        return []

    monkeypatch.setattr(voices, "list_voices", fake_list)
    opts = ReadOptions(**{**MULTI.__dict__, "voice": "nobody"})
    gen = FakeGen(tone, [])
    s = await read_aloud(plan_reading(STORY[:2500], opts), opts, tmp_path / "out", generator=gen)
    assert gen.calls == [] and not s.parts
    assert "navig audio voices" in (s.error or "")


async def test_the_default_voice_is_the_configured_one(tone, tmp_path, isolated_cache):
    opts = ReadOptions(lang="en", max_chunk_chars=600)
    gen = FakeGen(tone, [])
    await read_aloud(plan_reading(STORY[:1200], opts), opts, tmp_path / "out", generator=gen)
    assert gen.calls[0]["voice_id"] == _Config.tts_voice_id


async def test_a_missing_ffmpeg_stops_before_anything_is_spent(tmp_path, monkeypatch):
    monkeypatch.setattr(reader, "ffmpeg_available", lambda: False)
    gen = FakeGen(b"x", [])
    with pytest.raises(ReadError, match="ffmpeg"):
        await read_aloud(plan_reading(STORY[:1200]), ReadOptions(), tmp_path / "out", generator=gen)
    assert gen.calls == []


async def test_a_generator_the_caller_owns_is_not_closed(tone, tmp_path, isolated_cache):
    """The engine closes only a client it created itself — a caller reusing one client
    across several readings must not find it shut after the first."""
    gen = FakeGen(tone, [])
    await read_aloud(plan_reading(STORY[:1200], MULTI), MULTI, tmp_path / "out", generator=gen)
    assert gen.calls, "the reading must actually have run for this assertion to mean anything"
    assert gen.closed is False


# ── loudness normalisation must never cost the listener a stretch of the story ─


def _truncating_normalize(src, dst, *, i, **kw):
    """What ffmpeg's loudnorm was measured doing to short joins: a shorter file, exit 0."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-t", "0.3", str(dst)], check=True
    )


def _faithful_normalize(src, dst, *, i, **kw):
    Path(dst).write_bytes(Path(src).read_bytes())


async def test_a_normalisation_that_cuts_audio_falls_back_and_warns(
    tone, tmp_path, isolated_cache, monkeypatch, real_audio
):
    """Measured: loudnorm turned a 1.9 s join with silence gaps into 0.8 s, exit code 0.
    A shorter part is a hole in the story — keep the un-normalised audio and say so."""
    monkeypatch.setattr(reader, "normalize", _truncating_normalize)
    plan = _small_plan()
    s = await read_aloud(plan, MULTI, tmp_path / "out", generator=FakeGen(tone, []))

    assert s.complete, "a fallback is not a failure — the reading goes on"
    assert len(s.warnings) == plan.part_count and "shortened the audio" in s.warnings[0]
    for part, group in zip(s.parts, plan.parts):
        assert part.duration_s >= len(group) * 0.8, "the full joined audio must survive"
        assert part.path.exists() and part.path.stat().st_size > 0
    assert not list((tmp_path / "out").glob(".*joined*")), "the fallback must not leave scratch files"


async def test_a_faithful_normalisation_is_used_without_a_warning(
    tone, tmp_path, isolated_cache, monkeypatch
):
    """The no-fallback branch (fast: stand-in tools). Its sibling above proves the cut branch
    against real ffmpeg."""
    monkeypatch.setattr(reader, "normalize", _faithful_normalize)
    plan = _small_plan()
    s = await read_aloud(plan, MULTI, tmp_path / "out", generator=FakeGen(tone, []))
    assert s.complete and s.warnings == []
    assert not list((tmp_path / "out").glob(".*joined*"))
