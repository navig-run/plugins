"""Standalone entry points: ``navig-content`` (canonical) and ``navig-pipeline`` (old name).

Installs and runs without navig: the same commands navig mounts on ``navig content``
(``navig pipeline`` is a deprecated alias). Filming a live URL, ``telegram-import`` and
``schedule`` need navig and say so. One command surface, several doors.
"""

from __future__ import annotations

from navig_sdk.cli import run

from navig_pipeline.commands.pipeline import pipeline_app


def main() -> None:
    run(pipeline_app, "navig-pipeline")


def main_content() -> None:
    run(pipeline_app, "navig-content")


if __name__ == "__main__":
    main()
