"""``navig audio read`` — the terminal door to the bedtime reader.

The synthesis itself is covered by ``test_reader``; here the engine's ``read_aloud`` is a
stub, because the real one calls ElevenLabs and spends the owner's credits. What is pinned
is everything around it: the input forms, the encodings a real text file arrives in, the
quote, the ceiling that needs ``--yes``, the refusal when the account cannot afford it, and
the exit codes (usage = 2, failure = 1) a script can rely on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from navig_audio import reader, reading_job
from navig_audio.commands.audio import audio_app
from navig_audio.reader import ReadPart, ReadSummary, decode_text_file

RU = "Жили-были старик со старухой у самого синего моря. И не было у них ничего. " * 12
FR = "Il était une fois un roi qui n'avait pas de fils, et il le regrettait dans son cœur. " * 12
EN = "Once upon a time there was a king, and he was the best of all the kings in the land. " * 12


def run(*args: str, input: bytes | None = None):
    return CliRunner().invoke(audio_app, ["read", *args], input=input)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(reader.cache, "podcast_cache_dir", lambda: tmp_path / "cache")

    async def no_account():
        return None

    monkeypatch.setattr(reading_job, "_remaining_credits", no_account)

    def forbidden(*a, **k):
        raise AssertionError("read_aloud was called — this test must not spend credits")

    monkeypatch.setattr(reader, "read_aloud", forbidden)


def _fake_read(monkeypatch, *, parts=2, error=None, warnings=()):
    """Replace the engine's read_aloud with a stub; returns the list of calls it received."""
    calls: list[dict] = []

    async def fake(plan, options, out_dir, **kw):
        calls.append({"plan": plan, "options": options, "out_dir": out_dir, **kw})
        s = ReadSummary(out_dir=Path(out_dir), generated_chars=plan.chars, error=error)
        s.warnings.extend(warnings)
        for i in range(1, parts + 1):
            part = ReadPart(index=i, total=parts, path=Path(out_dir) / f"p{i}.mp3", duration_s=61.0, chars=10)
            s.parts.append(part)
            if kw.get("deliver"):
                await kw["deliver"](part)
        return s

    monkeypatch.setattr(reader, "read_aloud", fake)
    return calls


# ── input forms ───────────────────────────────────────────────────────────────


def test_no_input_is_a_usage_error_that_shows_the_way():
    r = run()
    assert r.exit_code == 2 and "Nothing to read" in r.output and "--file" in r.output


def test_text_and_file_together_are_refused(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello there", encoding="utf-8")
    r = run("some text", "--file", str(f))
    assert r.exit_code == 2 and "not both" in r.output


def test_a_missing_file_is_a_usage_error(tmp_path):
    r = run("--file", str(tmp_path / "nope.txt"))
    assert r.exit_code == 2 and "No such file" in r.output


def test_a_file_is_read_and_planned(tmp_path):
    f = tmp_path / "story.txt"
    f.write_text(EN, encoding="utf-8")
    r = run("--file", str(f), "--dry-run", "--json")
    data = json.loads(r.output)
    assert r.exit_code == 0 and data["lang"] == "en" and data["chars"] > 500


def test_stdin_is_read_when_the_argument_is_a_dash():
    r = run("-", "--dry-run", "--json", input=EN.encode("utf-8"))
    assert r.exit_code == 0 and json.loads(r.output)["lang"] == "en"


@pytest.mark.parametrize(
    ("text", "encoding", "lang"),
    [(RU, "utf-8", "ru"), (RU, "cp1251", "ru"), (FR, "utf-8", "fr"), (FR, "cp1252", "fr"), (RU, "utf-16", "ru")],
)
def test_a_text_file_in_any_common_code_page_is_decoded_correctly(tmp_path, text, encoding, lang):
    """A Russian .txt from an older Windows editor is cp1251 — read as UTF-8 it is a chapter
    of replacement characters that a voice then reads aloud."""
    f = tmp_path / "t.txt"
    f.write_bytes(text.encode(encoding))
    assert decode_text_file(f.read_bytes()).startswith(text[:30])
    r = run("--file", str(f), "--dry-run", "--json")
    assert r.exit_code == 0 and json.loads(r.output)["lang"] == lang


def test_a_utf8_bom_is_stripped():
    assert decode_text_file(b"\xef\xbb\xbfHello") == "Hello"


# ── the quote ─────────────────────────────────────────────────────────────────


def test_dry_run_quotes_the_cost_and_spends_nothing():
    r = run(EN, "--dry-run")
    assert r.exit_code == 0 and "Dry run" in r.output and "credits" in r.output


def test_dry_run_json_carries_the_spend_and_the_threshold():
    data = json.loads(run(EN, "--dry-run", "--json").output)
    assert data["spend"] == data["credits"] > 0
    assert data["confirm_over"] == reader.CONFIRM_CREDITS
    assert {"chars", "minutes", "parts", "lang", "speed", "model"} <= data.keys()


def test_options_flow_into_the_plan():
    data = json.loads(run(EN, "--dry-run", "--json", "--speed", "0.8", "--lang", "FR").output)
    assert data["speed"] == 0.8 and data["lang"] == "fr", "--lang is case-insensitive and wins"


def test_a_bad_speed_is_a_usage_error_not_a_crash():
    r = run(EN, "--dry-run", "--speed", "abc")
    assert r.exit_code == 2, r.output


# ── the ceiling and the account ───────────────────────────────────────────────

BIG = " ".join(f"Sentence number {n} of a very long story, told at length." for n in range(1, 900))


def test_a_big_reading_needs_an_explicit_yes():
    r = run(BIG)
    assert r.exit_code == 2 and "--yes" in r.output and "15,000" in r.output


def test_yes_goes_ahead(monkeypatch):
    calls = _fake_read(monkeypatch)
    r = run(BIG, "--yes")
    assert r.exit_code == 0 and len(calls) == 1


def test_a_reading_the_account_cannot_afford_is_refused_even_with_yes(monkeypatch):
    async def little():
        return 1_000

    monkeypatch.setattr(reading_job, "_remaining_credits", little)
    r = run(BIG, "--yes")
    assert r.exit_code == 1 and "1,000" in r.output, "overdrawing must be refused, not attempted"


# ── running it ────────────────────────────────────────────────────────────────


def test_a_reading_prints_each_part_and_the_spend(monkeypatch):
    _fake_read(monkeypatch, parts=2)
    r = run(EN)
    assert r.exit_code == 0
    assert "1/2" in r.output and "2/2" in r.output and "credits spent" in r.output


def test_the_output_goes_under_the_audio_dir_by_default_and_under_out_when_given(monkeypatch, tmp_path):
    calls = _fake_read(monkeypatch)
    run(EN)
    assert calls[0]["out_dir"].parent.name == "read"
    run(EN, "--out", str(tmp_path / "mine"))
    assert calls[1]["out_dir"].parent == (tmp_path / "mine").resolve()
    assert calls[1]["out_dir"].name.startswith("read-")


def test_json_result_is_machine_readable(monkeypatch):
    _fake_read(monkeypatch, parts=2, warnings=["part 1: loudness normalisation shortened the audio"])
    data = json.loads(run(EN, "--json").output)
    assert data["complete"] is True and len(data["parts"]) == 2
    assert data["credits_spent"] > 0 and data["warnings"]


def test_warnings_are_shown_to_a_human(monkeypatch):
    _fake_read(monkeypatch, warnings=["part 1: loudness normalisation shortened the audio"])
    assert "shortened the audio" in run(EN).output


def test_an_error_after_some_parts_exits_nonzero_and_says_so(monkeypatch):
    _fake_read(monkeypatch, parts=1, error="provider exploded")
    r = run(EN)
    assert r.exit_code == 1 and "provider exploded" in r.output and "stopped early" in r.output


def test_no_parts_at_all_exits_nonzero(monkeypatch):
    _fake_read(monkeypatch, parts=0)
    assert run(EN).exit_code == 1


def test_a_missing_ffmpeg_is_reported_with_the_fix(monkeypatch):
    async def boom(*a, **k):
        raise reader.ReadError("ffmpeg is not installed — scoop install ffmpeg")

    monkeypatch.setattr(reader, "read_aloud", boom)
    r = run(EN)
    assert r.exit_code == 1 and "scoop install ffmpeg" in r.output
