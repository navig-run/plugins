"""Standalone entry point for the ``navig-generate`` console script.

Installs and runs without navig: the same commands navig mounts on ``navig generate``.
One command surface, two front doors.
"""

from __future__ import annotations

from navig_sdk.cli import run

from navig_generate.commands.generate import generate_app


def main() -> None:
    run(generate_app, "navig-generate")


if __name__ == "__main__":
    main()
