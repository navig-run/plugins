"""The plugin must work next to navig 3.25.0 — the core its floor names.

Each helper newer than that release is imported through ``navig_cabinet._core`` with a
fallback. These simulate the older core by hiding the newer modules, and assert the
fallback does the job (or refuses with a clear message) instead of raising ImportError.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture
def old_core(monkeypatch):
    """Hide everything newer than navig 3.25.0, then reload the compat module."""
    for name in ("navig.platform.paths", "navig.core.coerce", "navig.messaging.notify_operator",
                 "navig.messaging", "navig.llm", "navig.llm.guard"):
        monkeypatch.setitem(sys.modules, name, None)  # `import` of a None entry raises ImportError
    import navig_cabinet._core as core

    reloaded = importlib.reload(core)
    yield reloaded
    monkeypatch.undo()
    importlib.reload(core)


def test_resolve_user_path_fallback_anchors_to_the_invocation_dir(old_core, tmp_path, monkeypatch):
    assert old_core.resolve_user_path.__module__ == "navig_cabinet._core"
    monkeypatch.setenv("NAVIG_INVOCATION_CWD", str(tmp_path))
    assert old_core.resolve_user_path("docs/a.pdf") == (tmp_path / "docs" / "a.pdf").resolve()
    absolute = tmp_path / "x.pdf"
    assert old_core.resolve_user_path(str(absolute)) == absolute.resolve()


@pytest.mark.parametrize("value,expected", [("8", 8), (7, 7), (None, 9), ("x", 9), (True, 9), (" 23 ", 23)])
def test_coerce_int_fallback(old_core, value, expected):
    assert old_core.coerce_int(value, default=9) == expected


def test_notify_reports_not_sent_instead_of_raising(old_core):
    assert old_core.notify_available() is False
    assert old_core.notify_operator("x") is False
    assert old_core.escape_html("<b>&") == "&lt;b&gt;&amp;"
    assert old_core.llm_guard() is None


def test_reply_refuses_clearly_on_an_old_core(old_core, tmp_path):
    from navig_cabinet.paperwork.commands.paperwork import paperwork_app
    from typer.testing import CliRunner

    space = tmp_path / "space"
    (space / ".navig").mkdir(parents=True)
    res = CliRunner().invoke(paperwork_app, ["reply", "letter.pdf", "--say", "ok", "--space", str(space)])
    assert res.exit_code == 1
    assert "newer navig core" in res.output


def test_every_newer_core_import_goes_through_the_compat_module():
    """The floor guard's rule, pinned here too: no direct import of these names anywhere
    else in the package — a new one would make the floor a false claim again."""
    pkg = Path(__file__).resolve().parents[1] / "navig_cabinet"
    offenders = []
    needles = ("navig.platform.paths import resolve_user_path", "navig.core.coerce import coerce_int",
               "navig.messaging.notify_operator import", "from navig.llm import guard")
    for f in pkg.rglob("*.py"):
        if f.name == "_core.py":
            continue
        text = f.read_text(encoding="utf-8")
        offenders += [f"{f.relative_to(pkg)}: {n}" for n in needles if n in text]
    assert not offenders, offenders
