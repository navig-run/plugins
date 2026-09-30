"""Shared fixtures.

``navig_contacts`` is editable-installed against the MAIN checkout; put THIS
worktree's package first so the tests exercise the code under test, not the
installed copy.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[1]
_CORE = _PLUGIN.parents[1] / "core"
_SDK = _PLUGIN.parents[1] / "registry" / "sdk" / "python"

sys.path.insert(0, str(_PLUGIN))
# navig and navig-sdk are installed from the MAIN checkout (or PyPI), so in a worktree
# the tests would import those rather than the ones they are being changed alongside --
# and navig.store.contacts is an alias of this package's store, built on navig-sdk.
for _dep in (_SDK / "navig_sdk", _CORE / "navig"):
    if (_dep / "__init__.py").exists():
        sys.path.insert(0, str(_dep.parent))

from navig_contacts.engine import db as db_mod  # noqa: E402

#: Real exports use CRLF and the parser has to cope, so the fixtures produce it.
CRLF = chr(13) + chr(10)


@pytest.fixture()
def book(tmp_path, monkeypatch):
    """A throwaway contact book, standing in for the global one."""
    path = tmp_path / "contacts.db"
    monkeypatch.setattr(db_mod, "global_book_path", lambda: path)
    db_mod.use_book(None)
    db_mod.init_db()
    yield path
    db_mod.use_book(None)


@pytest.fixture()
def space_book(tmp_path, monkeypatch):
    """A named space whose own book ``--space`` can reach."""
    root = tmp_path / "a-space"
    root.mkdir()

    class _Cfg:
        path = root

    monkeypatch.setattr("navig.spaces.resolver.discover_space_paths",
                        lambda *a, **k: {"a-space": _Cfg()}, raising=False)
    monkeypatch.setattr("navig.spaces.contracts.normalize_space_name",
                        lambda name: name, raising=False)
    monkeypatch.setattr(db_mod, "global_book_path",
                        lambda: tmp_path / "global.db")
    yield root
    db_mod.use_book(None)


def write_vcf(path: Path, cards: list[str]) -> Path:
    """Write a .vcf file from a list of card bodies."""
    blocks = []
    for body in cards:
        lines = ["BEGIN:VCARD", "VERSION:3.0"]
        lines += body.strip().splitlines()
        lines.append("END:VCARD")
        blocks.append(CRLF.join(lines))
    path.write_text(CRLF.join(blocks) + CRLF, encoding="utf-8")
    return path
