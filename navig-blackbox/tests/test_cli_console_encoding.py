"""Regression: the standalone CLI must survive a legacy Windows console.

`blackbox_app`'s Typer help contains emoji. On a console whose encoding is an ANSI/OEM code page
(cp1251, cp866, cp437 — anything but UTF-8) rich cannot encode them, and click raises
UnicodeEncodeError *while rendering `--help`*, before one line reaches the user. Measured
against the PUBLISHED wheel on cp1251: `nbb --help` tracebacked; the same command under
PYTHONUTF8=1 worked.

Inside navig it never happens — navig sets the console up. Standalone nothing does, and
standalone is precisely what this package promises. So the CLI reconfigures its own stdio.

Reproduced here by forcing PYTHONIOENCODING in a SUBPROCESS: the parent's streams are left
alone, and the child gets exactly the console this bug needs.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


def _run(encoding: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", "from navig_blackbox.cli import main; main()", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env={**_env(), "PYTHONIOENCODING": encoding},
    )


def _env() -> dict:
    import os

    e = dict(os.environ)
    # PYTHONUTF8 would mask the very thing under test.
    e.pop("PYTHONUTF8", None)
    return e


@pytest.mark.parametrize("encoding", ["cp1251", "cp866", "cp437"])
def test_help_survives_a_legacy_console(encoding: str) -> None:
    r = _run(encoding)
    assert "UnicodeEncodeError" not in (r.stderr or ""), (
        f"`nbb --help` crashed under PYTHONIOENCODING={encoding}:\n{r.stderr[-800:]}"
    )
    assert "Usage:" in (r.stdout or "") + (r.stderr or ""), (
        f"no usage text was produced under {encoding}; stderr:\n{r.stderr[-400:]}"
    )


def test_utf8_console_still_works() -> None:
    r = _run("utf-8")
    assert "UnicodeEncodeError" not in (r.stderr or "")
    assert "Usage:" in (r.stdout or "") + (r.stderr or "")


def test_the_guard_runs_before_the_app() -> None:
    """Ordering is the whole fix: reconfiguring after the first render is too late."""
    import inspect

    from navig_blackbox import cli

    src = inspect.getsource(cli.main)
    assert "_ensure_utf8_stdio()" in src, "the CLI no longer reconfigures its stdio"
    assert src.index("_ensure_utf8_stdio()") < src.index("blackbox_app()"), (
        "stdio must be reconfigured BEFORE the Typer app renders anything"
    )


def test_the_guard_never_raises() -> None:
    """Best-effort by contract — a console it cannot reconfigure must not kill the CLI."""
    from navig_blackbox import cli

    cli._ensure_utf8_stdio()
    cli._ensure_utf8_stdio()   # idempotent
