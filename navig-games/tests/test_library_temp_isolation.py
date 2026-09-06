"""Two overlapping library reads must not share one temp file.

`amazon.py` copied the vendor DB to a FIXED path — `<tmp>/navig_amazon_games.db` — and
deleted it in a `finally`. Two overlapping calls (the daemon and a CLI run, or two
`navig games` invocations) therefore shared one file: each `copy2` wrote over the copy the
other was reading, and whichever finished first unlinked it out from under the other.

Both failures land in `except (sqlite3.Error, OSError): return []`, so the symptom was
"you own no Amazon games" rather than an error — and `gog.py`, the sibling doing the same
job, already used `tempfile.mkstemp()` correctly.
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from navig_games.engine.library import amazon as amazon_lib
from navig_games.engine.library import gog as gog_lib


def _make_amazon_db(path: Path) -> Path:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE DbSet (ProductTitle TEXT, InstallDirectory TEXT, Id TEXT)"
    )
    con.execute("INSERT INTO DbSet VALUES ('Test Game', ?, 'p1')", (str(path.parent),))
    con.commit()
    con.close()
    return path


@pytest.mark.parametrize("module", [amazon_lib, gog_lib], ids=["amazon", "gog"])
def test_the_temp_copy_is_unique_per_call(module):
    """THE REGRESSION: a fixed temp filename is shared by every concurrent caller."""
    src = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    uses_mkstemp = any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "mkstemp"
        for n in ast.walk(tree)
    )
    assert uses_mkstemp, (
        f"{Path(module.__file__).name} must take a UNIQUE temp copy (tempfile.mkstemp). A fixed "
        "name in the shared temp dir is contended by every concurrent call, and this code "
        "unlinks it in a finally."
    )

    # And specifically: no constant filename joined onto the temp dir.
    assert "gettempdir()" not in src or "mkstemp" in src, (
        "gettempdir() with a constant name is the shape that caused this"
    )


def test_two_overlapping_amazon_reads_both_succeed(tmp_path):
    """Behavioural: read while a previous call's temp file would have existed."""
    db = _make_amazon_db(tmp_path / "GameInstallInfo.sqlite")

    first = amazon_lib.installed_amazon_games(db_path=db)
    second = amazon_lib.installed_amazon_games(db_path=db)

    assert [g.title for g in first] == ["Test Game"]
    assert [g.title for g in second] == ["Test Game"], (
        "a second read returned nothing — the temp copy is being reused or removed"
    )


def test_the_temp_copy_is_cleaned_up(tmp_path):
    """Unique must not mean leaked: the copy is still removed after each call."""
    import tempfile

    db = _make_amazon_db(tmp_path / "GameInstallInfo.sqlite")
    tmp_root = Path(tempfile.gettempdir())
    before = set(tmp_root.glob("navig_amazon_*"))

    amazon_lib.installed_amazon_games(db_path=db)

    leaked = set(tmp_root.glob("navig_amazon_*")) - before
    assert not leaked, f"temp copies left behind: {sorted(str(p) for p in leaked)}"


def test_a_missing_db_is_still_empty_not_an_error(tmp_path):
    """Anti-vacuity: the no-games path must stay quiet, not raise."""
    assert amazon_lib.installed_amazon_games(db_path=tmp_path / "nope.sqlite") == []
