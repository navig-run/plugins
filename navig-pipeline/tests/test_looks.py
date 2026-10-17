"""Formats as data.

The properties worth pinning are the ones whose failure is silent: a typo'd look name
that renders a plain video, an override that is quietly ignored, and a filter order that
produces a valid graph with the effects cancelling each other out.
"""

from __future__ import annotations

import pytest

from navig_pipeline.looks import (
    BROADCAST,
    FOUND_FOOTAGE,
    LOOKS,
    OPERATOR,
    UNDERGROUND,
    Look,
    resolve,
)


def test_every_builtin_is_registered_under_its_own_name() -> None:
    for name, look in LOOKS.items():
        assert look.name == name


def test_the_four_reference_formats_are_present() -> None:
    assert set(LOOKS) == {"broadcast", "operator", "underground", "found-footage"}


# ── resolution ────────────────────────────────────────────────────────────────


def test_no_look_named_is_simply_no_look() -> None:
    assert resolve(None) is None
    assert resolve("") is None


def test_an_unknown_look_is_an_error_not_a_silent_fallback() -> None:
    """A typo must not render a plain video that merely looks like a weak format."""
    with pytest.raises(KeyError, match="unknown look"):
        resolve("braodcast")


def test_the_error_lists_what_is_available() -> None:
    with pytest.raises(KeyError, match="broadcast"):
        resolve("nope")


def test_an_override_applies_without_mutating_the_builtin() -> None:
    tuned = resolve("broadcast", {"grain": 1.0})
    assert tuned.grain == 1.0
    assert BROADCAST.grain == 6.0, "the shared builtin was mutated"


def test_an_override_of_a_setting_that_does_not_exist_is_refused() -> None:
    # Otherwise a misspelled key is accepted and does nothing, which reads as the
    # setting having no effect rather than as a typo.
    with pytest.raises(KeyError, match="scanline"):
        resolve("broadcast", {"scanline": True})


# ── the composed filter ───────────────────────────────────────────────────────


def test_a_look_produces_a_non_empty_chain() -> None:
    for name in LOOKS:
        assert resolve(name).picture_filter(), f"{name} composed to nothing"


def test_motion_runs_before_texture() -> None:
    """Grain added before a scale is resampled into mush and reads as compression."""
    chain = UNDERGROUND.picture_filter(beats=[1.0], cuts=[2.0])
    assert chain.index("zoompan") < chain.index("noise")


def test_grade_runs_before_the_tape_pass() -> None:
    chain = BROADCAST.picture_filter()
    assert chain.index("eq=") < chain.index("chromashift")


def test_a_disabled_effect_leaves_no_trace_in_the_chain() -> None:
    # broadcast deliberately does not punch — a tape machine has no zoom.
    assert "zoompan" not in BROADCAST.picture_filter(beats=[1.0, 2.0])
    assert BROADCAST.punch == 0.0


def test_beats_only_matter_where_the_look_punches() -> None:
    with_beats = UNDERGROUND.picture_filter(beats=[1.0])
    without = UNDERGROUND.picture_filter(beats=[])
    assert with_beats != without
    assert "zoompan" in with_beats and "zoompan" not in without


def test_cuts_only_glitch_where_the_look_glitches() -> None:
    assert "rgbashift" in UNDERGROUND.picture_filter(cuts=[1.0])
    assert "rgbashift" not in BROADCAST.picture_filter(cuts=[1.0])


# ── the formats say what they are ────────────────────────────────────────────


def test_found_footage_frames_much_smaller_than_broadcast() -> None:
    # The small window IS the device — it must be visibly smaller, not marginally.
    assert FOUND_FOOTAGE.letterbox < BROADCAST.letterbox - 0.15


def test_broadcast_reads_monochrome_without_flattening_the_accent() -> None:
    """Near-monochrome is the goal; flat desaturation was the wrong way to reach it.

    Dropping `saturation` to 0.15 achieved the period look and erased the ketchup accent
    with it — measured on a rendered frame, the coral hoodie disappeared entirely. The
    greying is now done by `colorhold`, which spares the one colour the brand is built
    on, so `saturation` must stay high or it would undo that.
    """
    assert BROADCAST.accent == "0xD63A32"
    assert "colorhold" in BROADCAST.picture_filter()
    assert BROADCAST.saturation > 0.5, "a low saturation here would re-erase the accent"


def test_the_two_registers_are_assigned_not_left_blank() -> None:
    """Mixing registers in one reel reads as inconsistency; every look must pick one."""
    for look in LOOKS.values():
        assert look.register in {"universe-art", "sideeffects"}, look.name


def test_every_look_names_an_existing_art_preset() -> None:
    # A look carries the NAME of an art-direction block rather than restating it, so the
    # look and the prompt library cannot disagree about the palette.
    for look in LOOKS.values():
        assert look.preset and look.preset.startswith("SCH-"), look.name


def test_the_meme_format_is_the_only_one_in_the_joke_register() -> None:
    jokes = [n for n, look in LOOKS.items() if look.register == "universe-art"]
    assert jokes == ["underground"]


def test_a_look_is_immutable() -> None:
    with pytest.raises(Exception):
        OPERATOR.grain = 99  # type: ignore[misc]


def test_a_hand_built_look_needs_only_a_name_and_a_description() -> None:
    plain = Look(name="x", description="y")
    assert plain.picture_filter() == "" or isinstance(plain.picture_filter(), str)


def test_broadcast_keeps_the_brand_accent_alive() -> None:
    """The period grade must not erase the one colour the identity is built on."""
    assert BROADCAST.accent is not None
    chain = BROADCAST.picture_filter()
    assert "colorhold" in chain
    # And it must not ALSO flatten everything, which would undo the hold.
    assert BROADCAST.saturation > 0.5


def test_the_accent_hold_runs_before_the_grade() -> None:
    # Greying first and then holding would have nothing left to hold.
    chain = BROADCAST.picture_filter()
    assert chain.index("colorhold") < chain.index("eq=")


# ── a look for a project that is not this one ────────────────────────────────


def test_a_spec_look_is_built_but_never_registered() -> None:
    """The registry above is this universe's closed set; another project brings numbers.

    Without this seam the only way to grade a different project was to join `LOOKS`, which
    would have meant loosening the four-name, two-register and SCH- preset assertions that
    are the point of the tests above.
    """
    from navig_pipeline.looks import from_spec

    look = from_spec({"name": "flare", "description": "a road flare in a night wood",
                      "accent": "0xD62828", "grain": 9.0})
    assert look.name == "flare"
    assert "colorhold" in look.picture_filter()
    assert "flare" not in LOOKS


def test_a_spec_with_an_unknown_setting_is_refused() -> None:
    from navig_pipeline.looks import from_spec

    with pytest.raises(KeyError, match="grian"):
        from_spec({"name": "x", "grian": 9.0})


def test_a_spec_must_be_named() -> None:
    from navig_pipeline.looks import from_spec

    with pytest.raises(KeyError, match="name"):
        from_spec({"grain": 9.0})


def test_a_look_can_lift_the_floor_not_only_crush_it() -> None:
    """Without brightness a grade can only ever darken.

    Contrast pulls the shadows down and the vignette pulls the corners down. On art that
    is already dark the two compound, and the first real render of a still-based clip came
    out below what a phone shows at normal brightness with no knob to answer it.
    """
    from navig_pipeline.looks import from_spec

    lifted = from_spec({"name": "x", "brightness": 0.08})
    assert "brightness=0.08" in lifted.picture_filter()


def test_brightness_is_absent_from_every_existing_look() -> None:
    # Omitted at 0, so adding the knob cannot change a single rendered frame.
    for look in LOOKS.values():
        assert "brightness" not in look.picture_filter(), look.name
