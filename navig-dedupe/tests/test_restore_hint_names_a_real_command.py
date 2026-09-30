"""The quarantine message must name a restore command that actually runs.

It printed `dedupe restore <dir>` — not a program on its own (`ndup restore`) and not
the navig verb (`navig dedupe restore`).
"""

from __future__ import annotations

import sys

import pytest

from navig_dedupe.commands.dedupe import _restore_cmd


@pytest.mark.parametrize(
    "argv0,expected",
    [
        ("/usr/local/bin/ndup", "ndup restore"),
        ("/opt/venv/bin/navig-dedupe", "navig-dedupe restore"),
        ("/usr/local/bin/navig", "navig dedupe restore"),
    ],
)
def test_the_restore_hint_is_spelled_by_the_running_program(monkeypatch, argv0, expected) -> None:
    monkeypatch.setattr(sys, "argv", [argv0])
    assert _restore_cmd() == expected
