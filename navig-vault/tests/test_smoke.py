"""Smoke tests for the navig-vault scaffold."""

from __future__ import annotations

import navig_vault
from navig_vault.cli import main


def test_version_exposed() -> None:
    assert navig_vault.__version__


def test_cli_runs() -> None:
    """Bare invocation prints help and succeeds; --version exits 0 through argparse.

    Updated when the placeholder became a real CLI: argparse's `version` action raises
    SystemExit rather than returning, so asserting a return value here silently stopped
    describing the program the moment it grew a parser.
    """
    assert main([]) == 0

    import pytest

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
