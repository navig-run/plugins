"""Chunking, subtitles, cost and cache — the parts that must be right before spending.

None of these touch the network. They cover the arithmetic that a listener notices when
it is wrong: text silently dropped between chunks, subtitles drifting out of sync, or a
cost estimate that under-reports what a render will actually charge.
"""

from __future__ import annotations

import pytest

from navig_audio.podcast import cache, cost, srt
from navig_audio.podcast import chunker as ck
from navig_audio.podcast import scenario as sc

# ── chunking ───────────────────────────────────────────────────────────────────


def test_split_respects_the_limit_and_keeps_every_word() -> None:
    text = ("Ceci est une phrase de test. " * 40).strip()
    pieces = ck.split_text(text, max_chars=200)

    assert pieces, "a long text must produce chunks"
    assert all(len(p) <= 200 for p in pieces)
    # Losing text is the failure that would only be noticed on playback.
    assert "".join(pieces).replace(" ", "") == text.replace(" ", "")


def test_short_text_is_a_single_chunk() -> None:
    assert ck.split_text("Bonjour.", max_chars=200) == ["Bonjour."]


def test_split_packs_sentences_instead_of_one_per_request() -> None:
    # Every sentence as its own generation would be both expensive and seam-ridden.
    text = "Un. Deux. Trois. Quatre. Cinq. Six."
    assert len(ck.split_text(text, max_chars=100)) == 1


def test_a_word_longer_than_the_limit_still_splits() -> None:
    pieces = ck.split_text("a" * 500, max_chars=100)
    assert all(len(p) <= 100 for p in pieces)
    assert "".join(pieces) == "a" * 500


def test_empty_text_yields_nothing() -> None:
    assert ck.split_text("   \n  ") == []


def _episode_with_cue() -> sc.Episode:
    long_line = ("Phrase numero un. " * 30).strip()
    return sc.parse(
        "---\ndefault_speaker: NOBI\nvoices: {NOBI: v1, GUEST: v2}\n---\n"
        f"## 01 — Test\nNOBI: {long_line}\n\n"
        "[music: sting]\n\n"
        "NOBI: Deuxieme partie.\nGUEST: Une reponse.\n"
    )


def test_context_is_linked_between_consecutive_chunks() -> None:
    plan = ck.plan_track(_episode_with_cue().tracks[0], max_chars=200)
    chunks = [i for i in plan if isinstance(i, ck.Chunk)]

    assert chunks[0].previous_text is None
    assert chunks[0].next_text == chunks[1].text[: ck.CONTEXT_CHARS]
    assert chunks[1].previous_text is not None


def test_context_does_not_cross_a_cue() -> None:
    # Speech either side of a music sting is not continuous; conditioning across it
    # would make the model lean into a transition that is not there.
    plan = ck.plan_track(_episode_with_cue().tracks[0], max_chars=200)
    after_cue = [i for i in plan if isinstance(i, ck.Chunk)][-2]
    assert after_cue.previous_text is None


def test_context_does_not_cross_a_speaker_change() -> None:
    plan = ck.plan_track(_episode_with_cue().tracks[0], max_chars=200)
    guest = [i for i in plan if isinstance(i, ck.Chunk)][-1]
    assert guest.speaker == "GUEST"
    assert guest.previous_text is None


def test_cues_keep_their_position_in_the_plan() -> None:
    plan = ck.plan_track(_episode_with_cue().tracks[0], max_chars=200)
    kinds = ["cue" if isinstance(i, sc.Cue) else "chunk" for i in plan]
    assert kinds.index("cue") == len(kinds) - 3


# ── subtitles ──────────────────────────────────────────────────────────────────


def _alignment(text: str, step: float = 0.05):
    return (
        list(text),
        [i * step for i in range(len(text))],
        [(i + 1) * step for i in range(len(text))],
    )


def test_cues_keep_all_the_words() -> None:
    text = "Bonjour a tous. Bienvenue dans cette emission tres speciale du jour. Merci."
    cues = srt.cues_from_alignment(*_alignment(text), max_chars=40)
    assert " ".join(c.text for c in cues).replace(" ", "") == text.replace(" ", "")


def test_cues_are_monotonic_and_non_overlapping() -> None:
    text = "Un deux trois. Quatre cinq six sept huit. Neuf dix onze douze treize."
    cues = srt.cues_from_alignment(*_alignment(text), max_chars=30)
    for first, second in zip(cues, cues[1:]):
        assert first.start < first.end
        assert first.end <= second.start


def test_offset_shifts_every_cue() -> None:
    # This is the classic subtitle failure: chunk one looks perfect and everything
    # afterwards drifts, because the offset was not applied.
    text = "Une phrase de test."
    plain = srt.cues_from_alignment(*_alignment(text))
    shifted = srt.cues_from_alignment(*_alignment(text), offset=12.5)
    assert shifted[0].start == pytest.approx(plain[0].start + 12.5)
    assert shifted[0].end == pytest.approx(plain[0].end + 12.5)


def test_no_cue_splits_a_word() -> None:
    text = "anticonstitutionnellement voila un mot vraiment tres long a placer ici"
    cues = srt.cues_from_alignment(*_alignment(text), max_chars=30)
    assert cues, "cues was empty, so the loop below asserted nothing"
    for cue in cues:
        assert not cue.text.startswith(" ")
        assert cue.text == cue.text.strip()


def test_ragged_alignment_does_not_desynchronise() -> None:
    # A short array from the provider must not silently mis-time the rest of the track.
    chars, starts, ends = _alignment("Bonjour tout le monde.")
    cues = srt.cues_from_alignment(chars, starts[:5], ends[:5])
    assert cues
    assert all(c.end <= ends[4] for c in cues)


def test_timestamp_format_is_srt() -> None:
    assert srt.format_timestamp(0) == "00:00:00,000"
    assert srt.format_timestamp(3661.5) == "01:01:01,500"
    assert srt.format_timestamp(-1) == "00:00:00,000"


def test_srt_document_shape() -> None:
    cues = srt.cues_from_alignment(*_alignment("Bonjour."))
    document = srt.to_srt(cues)
    assert document.startswith("1\n")
    assert " --> " in document


def test_renumber_and_shift_compose() -> None:
    cues = srt.cues_from_alignment(*_alignment("Un. Deux. Trois."), max_chars=10)
    moved = srt.renumber(srt.shift(cues, 5.0), start=1)
    assert [c.index for c in moved] == list(range(1, len(moved) + 1))
    assert moved[0].start == pytest.approx(cues[0].start + 5.0)


# ── cost ───────────────────────────────────────────────────────────────────────


def _costed() -> sc.Episode:
    return sc.parse(
        "---\nlang: fr\ntitle: T\ndefault_speaker: HOST\nvoices: {HOST: v}\n---\n"
        "## 01 — A\nHOST: " + "x" * 100 + "\n\n"
        "> a note that costs nothing\n\n"
        "## 02 — B\nHOST: " + "y" * 200 + "\n"
    )


def test_estimate_counts_only_speech() -> None:
    projection = cost.estimate(_costed(), ["fr"])
    assert projection.chars_per_language == 300
    assert [t.chars for t in projection.tracks] == [100, 200]


def test_estimate_multiplies_by_language() -> None:
    projection = cost.estimate(_costed(), ["fr", "en"])
    assert projection.total_chars == 600
    assert projection.total_credits == 600


def test_flash_models_are_billed_at_half_rate() -> None:
    assert cost.credit_rate("eleven_flash_v2_5") == 0.5
    assert cost.credit_rate("eleven_multilingual_v2") == 1.0


def test_shortfall_is_reported_when_the_balance_is_too_small() -> None:
    projection = cost.estimate(_costed(), ["fr", "en"])
    tight = cost.Balance(tier="starter", used=29_900, limit=30_000,
                         can_clone_instant=True, can_clone_professional=False)
    message = cost.shortfall_message(projection, tight)
    assert message and "600" in message

    roomy = cost.Balance(tier="creator", used=0, limit=121_000,
                         can_clone_instant=True, can_clone_professional=True)
    assert cost.shortfall_message(projection, roomy) is None


def test_parse_balance_reads_the_fields_that_gate_a_render() -> None:
    balance = cost.parse_balance(
        {
            "tier": "free",
            "character_count": 500,
            "character_limit": 10_000,
            "can_use_instant_voice_cloning": False,
            "can_use_professional_voice_cloning": False,
        }
    )
    assert balance.remaining == 9_500
    assert not balance.can_clone_instant
    assert balance.covers(9_000) and not balance.covers(11_000)


# ── cache ──────────────────────────────────────────────────────────────────────


def _key(**overrides):
    base = dict(
        text="Bonjour.", voice_id="v1", model="eleven_multilingual_v2",
        output_format="mp3_44100_128", settings={"stability": 0.5},
        previous_text=None, next_text=None,
    )
    base.update(overrides)
    return cache.clip_key(**base)


def test_identical_requests_share_a_key() -> None:
    assert _key() == _key()


@pytest.mark.parametrize(
    "change",
    [
        {"text": "Bonsoir."},
        {"voice_id": "v2"},
        {"model": "eleven_flash_v2_5"},
        {"settings": {"stability": 0.9}},
        # Context changes the performance, so it must change the key — otherwise an
        # edited line would leave its neighbours sounding like the old version.
        {"previous_text": "quelque chose avant"},
        {"next_text": "quelque chose apres"},
    ],
)
def test_anything_that_changes_the_sound_changes_the_key(change: dict) -> None:
    assert _key(**change) != _key()


def test_store_and_locate_roundtrip(tmp_path) -> None:
    key = _key()
    alignment = {"characters": ["a"], "starts": [0.0], "ends": [0.1], "request_id": "r1"}
    cache.store(key, b"audio-bytes", alignment, root=tmp_path)

    clip = cache.locate(key, root=tmp_path)
    assert clip.exists
    assert clip.audio.read_bytes() == b"audio-bytes"
    assert clip.load_alignment() == alignment


def test_a_corrupt_sidecar_forces_a_re_render(tmp_path) -> None:
    # Serving subtitles that do not match the audio is worse than paying to regenerate.
    key = _key()
    cache.store(key, b"audio", {"characters": []}, root=tmp_path)
    clip = cache.locate(key, root=tmp_path)
    clip.alignment.write_text("{not json", encoding="utf-8")
    assert clip.load_alignment() is None


def test_stats_reports_an_empty_cache(tmp_path) -> None:
    assert cache.stats(tmp_path / "nope")["clips"] == 0
