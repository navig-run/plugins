"""`games check` on a narrow terminal must still show what the game IS.

At 44 columns the six-column table gave its one wrappable column — Title — zero width:
every row was a store glyph and an opaque `epic:cb733f…` key, no game name at all. Found
by capturing the command at phone width.
"""

from __future__ import annotations

import io

from rich.console import Console

from navig_games.commands.games import COMPACT_BELOW, _free_now_table

GAMES = [
    {"store": "epic", "title": "Monument Valley", "key": "epic:cb733f3432db46bca94965b0",
     "ends_in_days": 6, "claim_status": None, "original_price": "$3.99"},
    {"store": "itch", "title": "Bad [Cheese]", "key": "itch:2204", "ends_in_days": None},
]


def _render(width: int) -> str:
    buf = io.StringIO()
    Console(file=buf, width=width, color_system=None).print(_free_now_table(GAMES, "all", width=width))
    return buf.getvalue()


def test_titles_survive_a_phone_width() -> None:
    out = _render(44)
    assert "Monument Valley" in out
    assert "Bad [Cheese]" in out          # escaped, not eaten as markup
    # the claim handle is all there — folded onto the next line if it must, never cut with …
    assert "…" not in out
    assert "epic:cb733f" in out


def test_wide_terminals_keep_every_column() -> None:
    out = _render(COMPACT_BELOW + 40)
    for header in ("Store", "Title", "Was", "Ends", "Status", "Key"):
        assert header in out
