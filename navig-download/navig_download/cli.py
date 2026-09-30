"""Standalone entry point for the ``navig-download`` console script.

Installs and runs without navig: the same ``download_app`` Typer app navig mounts as
``navig download`` (and ``navig tt``). One command surface, two front doors.
"""

from __future__ import annotations

from navig_sdk.cli import run

from navig_download.commands.download import download_app


def main() -> None:
    run(download_app, "navig-download")


if __name__ == "__main__":
    main()
