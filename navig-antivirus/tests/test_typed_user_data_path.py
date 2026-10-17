"""A relative --user-data follows the directory the operator typed it in.

Inside navig the process cwd is the active SPACE (main.py chdir's there and stashes the
real directory in NAVIG_INVOCATION_CWD), so resolving against Path.cwd() named a folder in
the space: "User Data directory not found: demo-browser" while it sat right there.
"""

from __future__ import annotations

from pathlib import Path

from navig_antivirus.commands.antivirus import _resolve_ud


def test_relative_user_data_resolves_against_the_invocation_dir(tmp_path, monkeypatch) -> None:
    here, space = tmp_path / "project", tmp_path / "space"
    (here / "demo-browser").mkdir(parents=True)
    space.mkdir()
    monkeypatch.chdir(space)                       # where navig moved the process
    monkeypatch.setenv("NAVIG_INVOCATION_CWD", str(here))  # where the operator stood
    assert _resolve_ud("chrome", Path("demo-browser")) == here / "demo-browser"


def test_absolute_user_data_is_taken_as_given(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NAVIG_INVOCATION_CWD", str(tmp_path / "elsewhere"))
    assert _resolve_ud("chrome", tmp_path) == tmp_path


def test_standalone_without_navig_uses_the_process_cwd(tmp_path, monkeypatch) -> None:
    (tmp_path / "ud").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NAVIG_INVOCATION_CWD", raising=False)
    assert _resolve_ud("chrome", Path("ud")).resolve() == (tmp_path / "ud").resolve()
