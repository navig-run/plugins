"""`browser_running()` must survive console output that is not valid UTF-8.

``tasklist`` writes the OEM code page; ``subprocess(text=True)`` decodes with the ANSI one.
Measured on the real tool: 85 non-ASCII bytes in 6710 for one filtered query, and a strict
``utf-8`` decode of those bytes **raises**. This function wraps everything in
``except Exception: return False``, so a raise here does not surface — it becomes
"the browser is closed".

That answer is load-bearing for this plugin specifically: it is what tells the scanner the
browser's data files are safe to read. Being told "closed" while the browser is open is
exactly when those files are locked.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from navig_antivirus.engine import browsers

# Localized OEM header + the ASCII process row. Valid cp866/cp1251, invalid UTF-8.
_OEM_TASKLIST = b"\xc8\xec\xff \xe1\xa2\xe0\xa0\xa7\xa0\r\n=====\r\nchrome.exe   1234 Console\r\n"


def test_fixture_is_actually_undecodable_as_utf8() -> None:
    """Asserted, not assumed — an ASCII-ified fixture would prove nothing."""
    with pytest.raises(UnicodeDecodeError):
        _OEM_TASKLIST.decode("utf-8")


def test_non_utf8_output_still_detects_a_running_browser(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout=_OEM_TASKLIST, stderr=b"")

    monkeypatch.setattr(browsers.subprocess, "run", fake_run)

    assert browsers.browser_running("chrome") is True, (
        "an open browser was reported closed — its locked data files would then be read"
    )
    assert not seen.get("text"), "the probe must not request text mode"
    assert "encoding" not in seen, (
        "an encoding= here is the trap: utf-8 raises on OEM bytes and the except: turns "
        "that into 'closed'"
    )


def test_absent_browser_is_still_reported_absent(monkeypatch) -> None:
    """Anti-vacuity: the probe must not simply answer True."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        browsers.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, stdout=b"INFO: No tasks are running which match the criteria.\r\n", stderr=b""
        ),
    )
    assert browsers.browser_running("chrome") is False
