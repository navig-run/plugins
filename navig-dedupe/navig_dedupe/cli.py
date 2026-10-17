"""Standalone entry point for the ``navig-dedupe`` and ``ndup`` console scripts.

Installs and runs without navig — it simply invokes the same ``dedupe_app`` Typer
app that navig mounts as ``navig dedupe``. One command surface, two front doors.
"""
from __future__ import annotations

import sys

from navig_dedupe.commands.dedupe import dedupe_app


def _ensure_utf8_stdio() -> None:
    """Make the console able to print this CLI's own help text.

    The Typer app's help contains emoji. On a legacy Windows console — cp1251, cp866, cp437,
    whatever the machine's ANSI/OEM page is — rich cannot encode them and click raises
    UnicodeEncodeError *while rendering `--help`*, before a single line reaches the user.
    Measured on cp1251 against the PUBLISHED wheel: `ndup --help` tracebacks, and the
    same command under PYTHONUTF8=1 works.

    Inside navig this never happens, because navig sets the console up. Standalone there is
    nothing to do it — which is exactly the environment this package promises to work in.

    Best-effort by design: a stream that cannot be reconfigured (already detached, or not a
    real console) is left alone rather than taking the CLI down for a cosmetic concern.
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def main() -> None:
    _ensure_utf8_stdio()
    dedupe_app()


if __name__ == "__main__":
    main()
