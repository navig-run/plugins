"""Metadata rules that outrank the zero-shot guess.

Measured against the real 97,600-photo library, the CLIP-only classifier put
**7,421 files carrying camera EXIF into the webcam class** — a webcam frame
cannot have a Canon or iPhone Make/Model — while 5,812 files at exactly
1170x2532 (the iPhone 13/14 Pro screen) with zero camera EXIF sat unrecognised.
The metadata already knew. These tests keep it authoritative.
"""
from __future__ import annotations

import pytest

from navig_explore.vision import classify_rules as CR


def _c(w, h, ext=".jpg", camera="", overlay=False):
    return CR.classify_one(w=w, h=h, ext=ext, camera=camera, has_overlay_date=overlay)


# ── proof beats inference ───────────────────────────────────────────────────
def test_a_parsed_burned_in_timestamp_is_proof_of_a_webcam_frame():
    assert _c(999, 999, camera="", overlay=True) == ("webcam", "rule:overlay-timestamp")


def test_camera_exif_never_yields_screenshot_or_webcam():
    """The single biggest error: 7,421 camera files classed as webcam frames."""
    assert CR.forbidden_for(camera="Canon EOS 200D") == {"screenshot", "webcam"}
    assert CR.forbidden_for(camera="Apple iPhone 13 Pro") == {"screenshot", "webcam"}
    assert CR.forbidden_for(camera="") == frozenset()


def test_a_multi_megapixel_camera_file_is_not_a_web_graphic():
    """`web-graphic` means a banner, a logo, box art — not a 24-megapixel frame.

    Leaving the class available to camera files made it the runner-up in 4,864
    of the 10,833 `unsorted` ties, by far the largest single cause of that bucket.
    """
    assert "web-graphic" in CR.forbidden_for(camera="Canon EOS 200D", w=6000, h=4000)
    # A small camera JPEG really could have been re-saved as a web asset.
    assert "web-graphic" not in CR.forbidden_for(camera="Canon EOS 200D", w=640, h=480)
    # And without a camera the class stays available at any size.
    assert "web-graphic" not in CR.forbidden_for(camera="", w=6000, h=4000)


def test_a_camera_file_is_left_to_the_classifier():
    """A photo OF a document is still a document — pixels decide that one."""
    assert _c(4032, 3024, camera="Apple iPhone 13 Pro") is None
    assert _c(2592, 1944, camera="Canon EOS 200D") is None


# ── device screens ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(("w", "h"), [
    (1170, 2532),   # iPhone 13/14 Pro — 5,812 files, 0 with camera EXIF
    (1125, 2436),   # iPhone X/XS/11 Pro — 2,865, 0
    (640, 1136),    # iPhone SE — 1,005, 0
    (1080, 1920),   # 809 JPEGs, 0
])
def test_exact_phone_screen_size_is_a_screenshot(w, h):
    got = _c(w, h, ext=".png")
    assert got and got[0] == "screenshot"


def test_full_hd_LANDSCAPE_jpeg_is_not_a_screenshot():
    """Orientation is the discriminator, and getting it wrong swept in photos.

    Portrait 1080x1920 with no camera is a phone screenshot. LANDSCAPE 1920x1080
    is ordinary Full-HD photos and video frames — matching both orientations
    classified all of them as screenshots.
    """
    assert _c(1920, 1080, ext=".jpg") is None
    assert _c(1080, 1920, ext=".jpg")[0] == "screenshot"


def test_a_rotated_screenshot_still_counts_when_lossless():
    got = _c(2532, 1170, ext=".png")
    assert got and got[0] == "screenshot"


def test_desktop_size_needs_a_lossless_format():
    assert _c(2560, 1440, ext=".jpg") is None
    got = _c(2560, 1440, ext=".png")
    assert got and got[0] == "screenshot"


# ── webcam frame sizes ──────────────────────────────────────────────────────
@pytest.mark.parametrize(("w", "h"), [(320, 240), (352, 288), (176, 144)])
def test_small_frame_without_a_camera_is_a_webcam_frame(w, h):
    got = _c(w, h)
    assert got and got[0] == "webcam"


def test_a_small_frame_WITH_a_camera_is_not():
    assert _c(320, 240, camera="Canon EOS 200D") is None


def test_a_multi_megapixel_image_can_never_be_a_webcam_frame():
    """The leftovers in the webcam folder were a hand holding a device and a
    cracked phone screen — both multi-megapixel, neither a webcam."""
    assert "webcam" in CR.forbidden_for(camera="", w=4032, h=3024)
    assert "webcam" not in CR.forbidden_for(camera="", w=640, h=480)
    # …and the size rule alone must not also forbid screenshots.
    assert "screenshot" not in CR.forbidden_for(camera="", w=4032, h=3024)


# ── the honest fallback ─────────────────────────────────────────────────────
def test_unknown_shapes_defer_to_the_classifier():
    assert _c(1234, 987) is None


def test_fallback_class_exists():
    """Ambiguous files get a visible bucket, not a confident wrong folder."""
    assert CR.FALLBACK == "unsorted"


def test_screen_tables_do_not_overlap_with_webcam_sizes():
    """640x480 must not be both a webcam frame and a device screen."""
    assert not (CR.PHONE_SCREENS & CR.WEBCAM_SIZES)
    assert not (CR.DESKTOP_SCREENS & CR.WEBCAM_SIZES)


# ── how sure is sure enough ─────────────────────────────────────────────────
def test_confidence_is_a_probability_not_a_similarity_difference():
    """Why `unsorted` reached 10,833 files, 3,131 of them shot on a real camera.

    The old bar was an absolute gap of 0.010 between the best and second-best
    similarity. On this score scale the whole top1-to-top2 distribution has a
    median of 0.0219, so that "tiny" number rejected 24.5% of the library. Read
    as a softmax the same scores separate cleanly.
    """
    import numpy as np

    from navig_explore.vision import classify

    # A gap of 0.008 — well inside the old 0.010 margin, so it was called
    # ambiguous — is a decision once read at the model's own temperature.
    clear = np.array([0.118, 0.110, 0.070, 0.065, 0.060])
    assert float(clear[0] - clear[1]) < 0.010
    i, p = classify.confident(clear, bar=CR.MIN_CLASS_PROBABILITY)
    assert i == 0 and p > CR.MIN_CLASS_PROBABILITY

    tie = np.array([0.1104, 0.1100, 0.101, 0.099, 0.097])
    i, _p = classify.confident(tie, bar=CR.MIN_CLASS_PROBABILITY)
    assert i is None, "a near-tie with the runner-up must still refuse to answer"


def test_confidence_does_not_move_when_the_taxonomy_grows():
    """The defect that adding four screenshot types exposed.

    A bar on the FULL softmax silently tightens as candidates are added, because
    every extra one takes a share of the mass. Measured: four new types claimed
    382 captures out of `other` and pushed **1,253** already-labelled ones into
    it, spread across all twelve existing types — `other` went 35.7% -> 41.4%
    from a change meant to shrink it.

    Deciding between the top two removes the dependency entirely: the others
    cancel in the normaliser. It is also the better question — "photo or web
    graphic?" is not informed by how certainly the file is not a map.
    """
    import numpy as np

    from navig_explore.vision import classify

    # The added candidates score just under the runner-up — which is exactly what
    # happened: `notes` competes with `text`, `downloads` with `media`.
    small = np.array([0.118, 0.110, 0.070])
    grown = np.concatenate([small, np.full(13, 0.104)])

    i_s, p_s = classify.confident(small)
    i_g, p_g = classify.confident(grown)
    assert i_s == i_g == 0
    assert p_s == pytest.approx(p_g), "the extra candidates must not move the decision"

    # …whereas the full softmax this replaced would have collapsed it.
    def full_top(v):
        e = np.exp((v - v.max()) / classify.SOFTMAX_T)
        return float((e / e.sum()).max())

    assert full_top(grown) < full_top(small) - 0.1


def test_the_probability_bar_leaves_room_to_say_i_do_not_know():
    assert 0.5 <= CR.MIN_CLASS_PROBABILITY < 0.9, (
        "at 1.0 nothing is ever classified; below 0.5 the winner need not even "
        "be more likely than everything else combined")
