"""Standalone entry point for the ``navig-github`` console script.

Installs and runs without navig: the same ``github_app`` Typer app navig mounts as
``navig github``. One command surface, two front doors.
"""

from __future__ import annotations

from navig_sdk.cli import run

from navig_github.commands.github import github_app


def main() -> None:
    run(github_app, "navig-github")


if __name__ == "__main__":
    main()
