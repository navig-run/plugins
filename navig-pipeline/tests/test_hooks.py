"""Hooks: the ranking, and the claims it is allowed to make.

Everything here runs on a synthetic transcript with no audio, so the energy signal is
absent by design — which is itself one of the things worth testing, because the fallback
must re-weight rather than score every candidate's energy as zero.
"""

from __future__ import annotations

from dataclasses import dataclass

from navig_pipeline.hooks import (
    NGRAM,
    Passage,
    _spread,
    normalise,
    phrase_bounds,
    rank,
    repeat_score,
)


@dataclass
class W:
    text: str
    start: float
    end: float


@dataclass
class T:
    words: list


def line(words: str, at: float, *, rate: float = 0.4) -> list[W]:
    return [W(t, at + i * rate, at + i * rate + rate * 0.8)
            for i, t in enumerate(words.split())]


class TestPhraseBounds:
    def test_a_silence_splits_a_phrase(self):
        words = line("раз два три", 0.0) + line("четыре пять", 5.0)
        starts, ends = phrase_bounds(words)
        assert len(starts) == 2 and len(ends) == 2
        assert starts[1] == 5.0

    def test_the_first_and_last_word_always_bound_a_phrase(self):
        # Without this a track sung with no pause offers no legal window at all.
        words = line("непрерывный поток слов", 0.0)
        starts, ends = phrase_bounds(words)
        assert starts == [words[0].start]
        assert ends == [words[-1].end]

    def test_no_words_is_no_bounds(self):
        assert phrase_bounds([]) == ([], [])


class TestRepeatScore:
    def test_a_repeated_chorus_scores_high(self):
        chorus = "нет соединения нет ответа"
        words = line(chorus, 0.0) + line("совсем другая строка тут", 6.0) + line(chorus, 12.0)
        score = repeat_score(words, 0.0, 3.0)
        assert score > 0.9

    def test_a_unique_verse_scores_near_zero(self):
        words = line("одна единственная неповторимая строка", 0.0) + line("и другая", 6.0)
        assert repeat_score(words, 0.0, 3.0) == 0.0

    def test_a_window_too_short_for_an_ngram_scores_zero_not_one(self):
        words = line("два слова", 0.0)
        assert len(words) < NGRAM
        assert repeat_score(words, 0.0, 1.0) == 0.0

    def test_case_and_punctuation_do_not_break_a_match(self):
        words = line("Нет, соединения. Нет!", 0.0) + line("нет соединения нет", 6.0)
        assert repeat_score(words, 0.0, 3.0) > 0.0

    def test_normalise_strips_punctuation_and_case(self):
        assert normalise("«Нет,") == "нет"


class TestRank:
    def test_it_returns_passages_inside_the_requested_window(self):
        words = line("нет соединения нет ответа снова", 0.0) + line("и это всё что есть", 12.0)
        out = rank(T(words), None, top=5, min_s=3.0, max_s=20.0)
        assert out
        assert all(3.0 <= p.duration <= 20.0 for p in out)

    def test_without_audio_the_energy_signal_is_absent_not_zero(self):
        # Scoring a missing signal as 0 would flatten every candidate equally and quietly
        # turn the ranking into "whichever repeats most".
        words = line("нет соединения нет ответа", 0.0) + line("другая строка здесь", 8.0)
        out = rank(T(words), None, top=3, min_s=2.0, max_s=20.0)
        assert out
        assert "energy" not in out[0].parts

    def test_an_empty_transcript_ranks_nothing(self):
        assert rank(T([]), None) == []

    def test_no_window_fits_when_the_track_is_shorter_than_the_minimum(self):
        assert rank(T(line("коротко", 0.0)), None, min_s=30.0, max_s=60.0) == []

    def test_the_scores_that_produced_the_order_travel_with_the_passage(self):
        # The ranking has to be arguable: a bare number nobody can inspect invites being
        # believed. Every passage carries its components.
        words = line("нет соединения нет ответа", 0.0) + line("нет соединения нет", 8.0)
        out = rank(T(words), None, top=2, min_s=2.0, max_s=20.0)
        assert out and set(out[0].parts) >= {"repeat", "density", "clean"}


class TestSpread:
    def test_near_duplicates_of_a_chosen_passage_are_refused(self):
        best = Passage(start=0.0, end=10.0, text="a", score=1.0)
        overlapping = Passage(start=0.5, end=10.5, text="b", score=0.9)
        distinct = Passage(start=40.0, end=50.0, text="c", score=0.8)
        out = _spread([best, overlapping, distinct], top=3)
        assert [p.text for p in out] == ["a", "c"]

    def test_a_candidate_sharing_a_third_of_its_material_is_refused(self):
        # 0.5 was too loose: six candidates each overlapping its neighbour by 47% all
        # passed individually and the list became one half-minute viewed six ways.
        first = Passage(start=0.0, end=12.0, text="a", score=1.0)
        shifted = Passage(start=8.0, end=20.0, text="b", score=0.9)
        assert [p.text for p in _spread([first, shifted], top=2)] == ["a"]

    def test_it_stops_at_top(self):
        many = [Passage(start=i * 30.0, end=i * 30.0 + 10.0, text=str(i), score=1.0 - i / 10)
                for i in range(8)]
        assert len(_spread(many, top=3)) == 3


class TestAdaptivePhraseGap:
    """The threshold has to measure the track, not assume a number that suits one."""

    def test_a_track_with_real_pauses_keeps_the_default(self):
        from navig_pipeline.hooks import PHRASE_GAP_S, phrase_gap

        words = []
        for i in range(8):
            words += line("одна строка тут", i * 6.0)
        assert phrase_gap(words, want=4) == PHRASE_GAP_S

    def test_a_densely_sung_track_lowers_the_threshold(self):
        # Nothing here reaches 0.45s. A fixed threshold finds no phrase break at all and
        # the whole track becomes uncuttable -- which is what happened on b1ch.mp3.
        from navig_pipeline.hooks import PHRASE_GAP_S, phrase_gap

        words = [W(f"w{i}", i * 0.5, i * 0.5 + 0.25) for i in range(60)]
        gap = phrase_gap(words, want=6)
        assert gap < PHRASE_GAP_S

    def test_it_never_goes_below_the_articulation_floor(self):
        # Under ~0.18s a "pause" is the space between syllables; cutting there is a
        # dropout, not an edit.
        from navig_pipeline.hooks import MIN_PHRASE_GAP_S, phrase_gap

        words = [W(f"w{i}", i * 0.30, i * 0.30 + 0.29) for i in range(40)]
        assert phrase_gap(words, want=20) >= MIN_PHRASE_GAP_S

    def test_a_dense_track_now_yields_windows_across_its_whole_length(self):
        # The regression this exists for: the back half of a dense track used to offer
        # nothing, and the output gave no hint that a setting rather than the song was
        # responsible.
        words = [W("нет" if i % 7 else "знаешь", i * 0.5, i * 0.5 + 0.3) for i in range(240)]
        out = rank(T(words), None, top=6, min_s=10.0, max_s=25.0)
        # Before the fix a uniformly dense track had NO phrase break at all: the only
        # window was the whole 120s, longer than max_s, so NOTHING came back.
        assert len(out) >= 4
        # And they spread. Six ~10s windows that may share a fifth of their material step
        # about 8.5s each, so ~50s of span is the most this can reach -- 40 is a floor
        # that a regression back to one clustered moment could not clear.
        assert max(p.end for p in out) - min(p.start for p in out) > 40.0
