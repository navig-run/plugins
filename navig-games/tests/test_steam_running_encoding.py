"""`is_steam_running()` must survive console output that is not valid UTF-8.

``tasklist`` writes the OEM code page; ``subprocess(text=True)`` decodes with the ANSI one,
and a strict ``utf-8`` decode of real output **raises** (measured: 85 non-ASCII bytes in
6710 for one filtered query). The function swallows exceptions into ``False``, so the raise
would not surface — it would read as "Steam is not running".

That is the answer that decides whether shortcuts are written, and writing them under a
live client is what this check exists to prevent.
"""

from __future__ import annotations

import subprocess

import pytest

from navig_games.engine.library import steam

# Localized OEM header + the ASCII process row. Valid cp866/cp1251, invalid UTF-8.
_OEM_TASKLIST = b"\xc8\xec\xff \xe1\xa2\xe0\xa0\xa7\xa0\r\n=====\r\nsteam.exe   4321 Console\r\n"


def test_fixture_is_actually_undecodable_as_utf8() -> None:
    with pytest.raises(UnicodeDecodeError):
        _OEM_TASKLIST.decode("utf-8")


def test_non_utf8_output_still_detects_a_running_client(monkeypatch) -> None:
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout=_OEM_TASKLIST, stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert steam.is_steam_running() is True, (
        "a live Steam client was reported as not running — shortcuts would be overwritten "
        "underneath it"
    )
    assert not seen.get("text"), "the probe must not request text mode"
    assert "encoding" not in seen, (
        "an encoding= here is the trap: utf-8 raises on OEM bytes and the except: turns "
        "that into 'not running'"
    )


def test_absent_client_is_still_reported_absent(monkeypatch) -> None:
    """Anti-vacuity: the probe must not simply answer True."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, stdout=b"INFO: No tasks are running which match the criteria.\r\n", stderr=b""
        ),
    )
    assert steam.is_steam_running() is False
