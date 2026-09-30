"""Standalone, the space is the nearest PROJECT ``.navig/`` — never navig's global layer.

``~/.navig`` holds navig's global config, so the walk up from any folder under home used to
stop there and file every variant under the home directory. navig's own resolver skips it;
the standalone twin must too.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from navig_generate import _compat


@pytest.fixture
def standalone(tmp_path, monkeypatch):
    """No navig spaces module, a home holding a global ``.navig/``, a separate config dir.

    Returns the folder to work under. On a machine where ``tmp_path`` already sits below a real
    ``~/.navig`` (a Windows temp dir under the user profile), THAT ancestor is home — a fake
    home deeper down would leave the real one on the walk, which is the case under test.
    """
    monkeypatch.setitem(sys.modules, "navig.spaces.active", None)  # import -> ImportError
    base = tmp_path / "home"
    base.mkdir()
    home = next((d for d in tmp_path.parents if (d / ".navig").is_dir()), None)
    if home is None:
        home = base
        (home / ".navig").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("NAVIG_CONFIG_DIR", str(cfg))
    return base


def _from(monkeypatch, where: Path) -> Path:
    where.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("NAVIG_INVOCATION_CWD", str(where))
    return _compat.active_working_dir()


def test_a_folder_under_home_is_not_filed_under_home(standalone, monkeypatch):
    here = standalone / "Pictures" / "shoot"
    assert _from(monkeypatch, here) == here


def test_a_project_navig_under_home_is_still_found(standalone, monkeypatch):
    project = standalone / "code" / "game"
    (project / ".navig").mkdir(parents=True)
    assert _from(monkeypatch, project / "assets" / "sprites") == project


def test_the_global_config_dir_is_skipped_too(standalone, monkeypatch, tmp_path):
    root = tmp_path / "elsewhere"
    (root / ".navig").mkdir(parents=True)
    monkeypatch.setenv("NAVIG_CONFIG_DIR", str(root / ".navig"))
    here = root / "work"
    assert _from(monkeypatch, here) == here
