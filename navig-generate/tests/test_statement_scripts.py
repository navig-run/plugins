"""``navig generate statement`` — scripts the display face cannot draw.

Druk Medium (the headline face) has **no Cyrillic**: a Russian line rendered
through it drew nothing at all — digits and punctuation survived, every letter
was an empty glyph (measured: `268` = 466k px of ink, `ГОРОДОВ` = 0). The
channel's own kinetic renderer already documents it (`F_SLAM = BebasNeue Bold
# has Cyrillic; Druk does not`), but the statement verb hardcoded Druk, so every
Russian card came out blank.

The face is now chosen by **glyph coverage**, not by script name: Druk first,
then Bebas Neue Bold, then Inter. Latin (incl. French accents) therefore still
renders through Druk — byte-identical to before.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL", reason="Pillow not installed")
from PIL import Image  # noqa: E402

from navig_generate import statement as st  # noqa: E402

BEBAS = getattr(st, "BEBAS", st.USR + "BebasNeue Bold.ttf")
channel_fonts = pytest.mark.skipif(
    not (Path(st.DRUK).exists() and Path(BEBAS).exists()),
    reason="the channel's Druk / Bebas faces are not installed here",
)

PLAIN = {"size": 86, "hud": False, "mark": False, "grid": False}


def _ink(png: Path) -> int:
    """Pixels of the card that actually carry something (alpha > 24)."""
    h = Image.open(png).convert("RGBA").getchannel("A").histogram()
    return sum(h[25:])


# ── the regression: this fails on the old renderer, with no new API involved ──
@channel_fonts
def test_a_cyrillic_headline_is_not_blank(tmp_path: Path):
    """The bug itself. Before the fix a Cyrillic-only line left the card empty."""
    out = st.render(st.prepare_line({**PLAIN, "line": "ГОРОДОВ НА КАРТЕ"}), tmp_path / "ru.png")
    assert _ink(out) > 5_000, "the Russian headline drew nothing — Druk has no Cyrillic"


@channel_fonts
def test_cyrillic_ink_is_comparable_to_the_same_length_in_latin(tmp_path: Path):
    ru = _ink(st.render(st.prepare_line({**PLAIN, "line": "ПЕРВЫЙ АРХИТЕКТОР"}), tmp_path / "ru.png"))
    en = _ink(st.render(st.prepare_line({**PLAIN, "line": "THE FIRST ARCHITECT"}), tmp_path / "en.png"))
    assert ru > 0.5 * en, (ru, en)


@channel_fonts
def test_red_segments_survive_the_fallback_face(tmp_path: Path):
    plain = st.render(st.prepare_line({**PLAIN, "line": "НУЛЕВАЯ ЗОНА"}), tmp_path / "a.png")
    red = st.render(st.prepare_line({**PLAIN, "line": "НУЛЕВАЯ *ЗОНА*"}), tmp_path / "b.png")
    assert plain.read_bytes() != red.read_bytes()


# ── the selection ────────────────────────────────────────────────────────────
@channel_fonts
def test_druk_has_no_cyrillic_and_bebas_has():
    """The premise, pinned so a font swap cannot silently re-break it."""
    assert not st.covers(st.DRUK, "ГОРОДОВ")
    assert st.covers(st.DRUK, "GRID")
    assert st.covers(BEBAS, "ГОРОДОВ")


@channel_fonts
def test_latin_and_french_stay_on_druk():
    chain = [st.DRUK, BEBAS, st.INTER_SB]
    for line in ("266 OF 268", "ZONE ZÉRO. ŒUVRE «CITÉ» — À ÇA", "PAS DE DIEUX. PAS DE ROIS."):
        assert st.pick_font(chain, line) == st.DRUK, line


@channel_fonts
def test_cyrillic_and_mixed_lines_fall_through_to_bebas():
    chain = [st.DRUK, BEBAS, st.INTER_SB]
    assert st.pick_font(chain, "ПЕРВЫЙ АРХИТЕКТОР") == BEBAS
    assert st.pick_font(chain, "WAVE 1 · НУЛЕВАЯ ЗОНА") == BEBAS     # one stray script moves the whole line


def test_pick_font_takes_the_first_face_that_covers_and_never_raises(monkeypatch: pytest.MonkeyPatch):
    """Hermetic: no real faces needed."""
    chain = ["a", "b", "c"]
    monkeypatch.setattr(st, "covers", lambda path, text: path == "b")
    assert st.pick_font(chain, "anything") == "b"
    monkeypatch.setattr(st, "covers", lambda path, text: False)
    assert st.pick_font(chain, "anything") == "c"                    # last face: draws SOMETHING, never aborts


def test_covers_ignores_whitespace_and_a_missing_face(tmp_path: Path):
    assert st.covers(str(tmp_path / "no-such-font.ttf"), "ABC") is False
    # whitespace has no ink in any face and must never count as "missing"
    assert st.covers(st.INTER_SB, "   ") is True or not Path(st.INTER_SB).exists()


# ── Latin parity: the fix must not move a single byte ────────────────────────
@channel_fonts
@pytest.mark.parametrize("line", [
    "A CITY LIGHTS WHEN *5 PEOPLE* CLAIM IT",
    "ZONE ZÉRO. *LE PREMIER ARCHITECTE.*",
    "PAS DE DIEUX. PAS DE ROIS. *SEULEMENT DES CITOYENS.*",
])
def test_latin_render_is_byte_identical_to_forcing_druk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line: str):
    """Selecting by coverage must change nothing for text Druk already drew."""
    spec = st.prepare_line({"line": line, "sub": "Genèse · extrait", "tag": "transmission",
                            "size": 80, "coords": "00°00′N 00°00′E", "wave": "WAVE 1"})
    chosen = st.render(spec, tmp_path / "chosen.png").read_bytes()
    monkeypatch.setattr(st, "pick_font", lambda paths, text: paths[0])      # old behaviour: always the first face
    forced = st.render(spec, tmp_path / "forced.png").read_bytes()
    assert chosen == forced
