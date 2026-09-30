"""Standalone entry point for the ``navig-pipeline`` console script.

Installs and runs without navig: the same commands navig mounts on ``navig pipeline``.
Filming a live URL and ``schedule`` need navig and say so. One command surface, two doors.
"""

from __future__ import annotations

from navig_sdk.cli import run

from navig_pipeline.commands.pipeline import pipeline_app


def main() -> None:
    run(pipeline_app, "navig-pipeline")


if __name__ == "__main__":
    main()
