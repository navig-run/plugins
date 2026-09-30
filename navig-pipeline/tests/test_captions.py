"""Captions: the grouping, the SRT, and the hallucination filter.

No network and no API key — :func:`parse_response` takes Whisper's payload shape directly,
which is where all the behaviour worth testing lives.
"""

from __future__ import annotations

import pytest

from navig_pipeline.captions import (
    LINE_GAP_S,
    MAX_LINE_CHARS,
    Line,
    Word,
    group_words,
    parse_response,
    slice_lines,
    to_srt,
)


def w(text: str, start: float, end: float) -> Word:
    return Word(text=text, start=start, end=end)


def payload(words, **extra):
    return {"language": "ru", "duration": 10.0,
            "words": [{"word": t, "start": s, "end": e} for t, s, e in words], **extra}


class TestGrouping:
    def test_a_pause_starts_a_new_line(self):
        words = [w("раз", 0.0, 0.3), w("два", 0.35, 0.6),
                 w("три", 0.6 + LINE_GAP_S + 0.1, 2.0)]
        lines = group_words(words)
        assert len(lines) == 2
        assert lines[0].text == "раз два"
        assert lines[1].text == "три"

    def test_a_long_line_is_broken_on_width(self):
        words = [w("слово", i * 0.2, i * 0.2 + 0.15) for i in range(20)]
        lines = group_words(words)
        assert len(lines) > 1
        assert all(len(ln.text) <= MAX_LINE_CHARS + len("слово") for ln in lines)

    def test_a_slow_sparse_line_is_broken_on_time(self):
        # Six short words over nine seconds: narrow enough to pass the width rule, and far
        # too long to leave on screen. Without the time rule this is one frozen caption.
        words = [w("да", i * 1.5, i * 1.5 + 0.2) for i in range(6)]
        lines = group_words(words)
        assert len(lines) > 1

    def test_sentence_punctuation_ends_a_line(self):
        words = [w("стоп.", 0.0, 0.4), w("дальше", 0.45, 0.9)]
        assert [ln.text for ln in group_words(words)] == ["стоп.", "дальше"]

    def test_no_words_is_no_lines(self):
        assert group_words([]) == []


class TestHallucinationFilter:
    @pytest.mark.parametrize("text", [
        "Редактор субтитров А.Синецкая",
        "Субтитры сделал DimaTorzok",
        "Subtitles by the Amara.org community",
        "Продолжение следует...",
    ])
    def test_known_credit_lines_are_dropped_and_reported(self, text):
        out = parse_response(payload([(text, 8.0, 9.5)]))
        assert out.lines == []
        # Reported, never silent: a filter that eats a real lyric without saying so is
        # worse than the hallucination it was added to prevent.
        assert out.dropped == [text]

    def test_a_real_lyric_that_merely_mentions_subtitles_survives(self):
        out = parse_response(payload([("а", 0.0, 0.2), ("на", 0.3, 0.5),
                                      ("экране", 0.6, 1.0), ("субтитры", 1.1, 1.6)]))
        assert out.dropped == []
        assert "субтитры" in out.text


class TestParseResponse:
    def test_word_timings_are_preferred(self):
        out = parse_response(payload([("раз", 0.0, 0.4), ("два", 0.5, 0.9)]))
        assert out.word_count == 2
        assert out.language == "ru"

    def test_segments_are_the_fallback_when_no_words_come_back(self):
        out = parse_response({"language": "fr", "duration": 5.0, "words": [],
                              "segments": [{"text": " une ligne ", "start": 0.0, "end": 2.0}]})
        assert out.word_count == 1
        assert out.words[0].text == "une ligne"

    def test_an_empty_payload_yields_an_empty_transcript(self):
        out = parse_response({})
        assert out.words == [] and out.lines == []


class TestSrt:
    def test_timestamps_are_subrip_shaped(self):
        srt = to_srt([Line(text="привет", start=61.5, end=63.25)])
        assert "00:01:01,500 --> 00:01:03,250" in srt
        assert srt.splitlines()[0] == "1"

    def test_offset_rebases_an_excerpt_onto_its_own_zero(self):
        # The whole point for a passage: words cut from 41s in must start near 0.
        srt = to_srt([Line(text="хук", start=41.2, end=43.0)], offset=41.2)
        assert "00:00:00,000 --> 00:00:01,800" in srt

    def test_an_offset_past_the_line_clamps_rather_than_going_negative(self):
        srt = to_srt([Line(text="x", start=1.0, end=2.0)], offset=5.0)
        assert "-" not in srt.split("-->")[0].splitlines()[-1]


class TestSliceLines:
    def test_a_line_overlapping_the_window_is_kept(self):
        lines = [Line("а", 0.0, 5.0), Line("б", 10.0, 12.0), Line("в", 20.0, 22.0)]
        kept = slice_lines(lines, 4.0, 21.0)
        # "а" straddles the start and is kept on purpose: a clip that opens on a voice
        # with no words looks broken.
        assert [ln.text for ln in kept] == ["а", "б", "в"]

    def test_lines_outside_the_window_are_dropped(self):
        lines = [Line("а", 0.0, 5.0), Line("б", 30.0, 32.0)]
        assert [ln.text for ln in slice_lines(lines, 10.0, 20.0)] == []


class TestChunkBounds:
    def test_a_short_file_is_one_window(self):
        from navig_pipeline.captions import CHUNK_S, chunk_bounds

        assert chunk_bounds(CHUNK_S - 1) == [(0.0, CHUNK_S - 1)]

    def test_windows_overlap_so_a_word_on_a_boundary_is_heard_whole(self):
        from navig_pipeline.captions import CHUNK_OVERLAP_S, chunk_bounds

        b = chunk_bounds(90.0)
        assert len(b) > 1
        for prev, nxt in zip(b, b[1:]):
            assert nxt[0] < prev[1], "windows must overlap"
            assert prev[1] - nxt[0] == pytest.approx(CHUNK_OVERLAP_S, abs=0.01)

    def test_the_windows_cover_the_whole_file(self):
        b = chunk_bounds_or(90.0)
        assert b[0][0] == 0.0
        assert b[-1][1] == pytest.approx(90.0)

    def test_a_final_sliver_is_absorbed_rather_than_sent_alone(self):
        # A two-second last chunk gives Whisper no context and is where a dropped final
        # line comes from.
        from navig_pipeline.captions import CHUNK_S, chunk_bounds

        for total in (52.0, 77.0, 102.0):
            b = chunk_bounds(total)
            assert b[-1][1] - b[-1][0] >= CHUNK_S / 3, f"sliver at {total}s: {b[-1]}"


def chunk_bounds_or(d):
    from navig_pipeline.captions import chunk_bounds

    return chunk_bounds(d)


class TestMergeTranscripts:
    def _t(self, words, language="en"):
        from navig_pipeline.captions import Transcript, group_words

        ws = [Word(t, s, e) for t, s, e in words]
        return Transcript(language=language, duration=(ws[-1].end if ws else 0.0),
                          words=ws, lines=group_words(ws), text=" ".join(w[0] for w in words))

    def test_offsets_are_applied_so_chunk_two_lands_in_real_time(self):
        from navig_pipeline.captions import merge_transcripts

        a = self._t([("one", 0.0, 0.5)])
        b = self._t([("two", 0.5, 1.0)])
        out = merge_transcripts([(0.0, a), (25.0, b)])
        assert [w.text for w in out.words] == ["one", "two"]
        assert out.words[1].start == pytest.approx(25.5)

    def test_a_word_heard_twice_in_the_overlap_is_kept_once(self):
        from navig_pipeline.captions import merge_transcripts

        a = self._t([("dead", 0.0, 1.0), ("yeah", 1.2, 2.0)])
        # chunk two starts at 1.0s and hears "yeah" again at its own 0.2s
        b = self._t([("yeah", 0.2, 1.0), ("more", 1.5, 2.0)])
        out = merge_transcripts([(0.0, a), (1.0, b)])
        assert [w.text for w in out.words] == ["dead", "yeah", "more"]

    def test_the_language_of_the_first_chunk_that_knows_wins(self):
        from navig_pipeline.captions import merge_transcripts

        a = self._t([("x", 0.0, 0.2)], language="unknown")
        b = self._t([("y", 0.0, 0.2)], language="ru")
        assert merge_transcripts([(0.0, a), (10.0, b)]).language == "ru"

    def test_hallucinations_are_still_filtered_after_merging(self):
        from navig_pipeline.captions import merge_transcripts

        a = self._t([("реальная", 0.0, 0.6), ("строка", 0.7, 1.2)])
        b = self._t([("Субтитры", 0.0, 0.8), ("сделал", 0.9, 1.6), ("DimaTorzok", 1.7, 2.4)])
        out = merge_transcripts([(0.0, a), (10.0, b)])
        assert out.dropped, "the credit line must be reported"
        assert all("Субтитры" not in ln.text for ln in out.lines)

    def test_merging_nothing_yields_an_empty_transcript_not_a_crash(self):
        from navig_pipeline.captions import merge_transcripts

        out = merge_transcripts([])
        assert out.words == [] and out.lines == []


# ── transcription is navig-audio's ─────────────────────────────────────────────


def test_transcribe_one_asks_navig_audio_for_word_timings(monkeypatch, tmp_path):
    """Captions no longer carry their own Whisper client: navig-audio transcribes (with a key
    or, without one, locally), and captions turn its payload into lines."""
    from navig_audio.voice import words
    from navig_pipeline import captions

    seen = {}

    def _words(audio, *, language=None, engine=None, timeout=600, model=None):
        seen.update(audio=audio, language=language, engine=engine)
        return {"language": "en", "words": [{"word": "hello", "start": 0.0, "end": 0.5},
                                            {"word": "world", "start": 0.6, "end": 1.0}]}

    monkeypatch.setattr(words, "transcribe_words", _words)
    clip = tmp_path / "a.mp3"
    clip.write_bytes(b"x")
    t = captions.transcribe_one(clip, language="en", engine="whisper_local")
    assert [w.text for w in t.words] == ["hello", "world"]
    assert seen == {"audio": clip, "language": "en", "engine": "whisper_local"}


def test_a_navig_audio_failure_is_a_caption_error_that_keeps_its_advice(monkeypatch, tmp_path):
    from navig_audio.voice import words
    from navig_pipeline import captions

    def _fail(*a, **k):
        raise words.WordsError("no OpenAI key - … or transcribe locally with faster-whisper")

    monkeypatch.setattr(words, "transcribe_words", _fail)
    clip = tmp_path / "a.mp3"
    clip.write_bytes(b"x")
    with pytest.raises(captions.CaptionError, match="faster-whisper"):
        captions.transcribe_one(clip)


def test_an_empty_transcript_is_an_error_not_silence(monkeypatch, tmp_path):
    from navig_audio.voice import words
    from navig_pipeline import captions

    monkeypatch.setattr(words, "transcribe_words", lambda *a, **k: {"words": [], "segments": []})
    clip = tmp_path / "a.mp3"
    clip.write_bytes(b"x")
    with pytest.raises(captions.CaptionError, match="transcribed to nothing"):
        captions.transcribe_one(clip)
