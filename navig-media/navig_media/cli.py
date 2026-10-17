"""``navig-media`` — report what of the media suite is installed.

Reads the environment only (importlib.metadata), so it works with navig absent and
never launches anything. Exit code 1 when a suite component is missing, so scripts
can assert a complete install.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, version

# (distribution, what it does, standalone command, the verb it mounts inside navig)
SUITE: tuple[tuple[str, str, str, str], ...] = (
    ("navig-generate", "image + video generation", "navig-generate", "navig generate"),
    ("navig-audio", "voice, music, sound effects", "navig-audio", "navig audio"),
    ("navig-text", "drafts, captions, lyrics", "navig-text", "navig text"),
    ("navig-video", "edit, render and encode footage", "navig-video", "navig video"),
    ("navig-pipeline", "content production: script → narrate → cut → publish", "navig-pipeline", "navig content"),
    ("navig-download", "acquire media from the web", "navig-download", "navig download"),
    ("navig-social", "publish to networks", "navig-social", "navig social"),
)


@dataclass
class Component:
    """One suite component and what the environment says about it."""

    package: str
    does: str
    standalone: str
    navig: str
    installed: str | None  # version when present, else None


def scan() -> list[Component]:
    """Probe the environment for every suite component."""
    out: list[Component] = []
    for package, does, standalone, verb in SUITE:
        try:
            ver: str | None = version(package)
        except PackageNotFoundError:
            ver = None
        out.append(Component(package, does, standalone, verb, ver))
    return out


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args
    unknown = [a for a in args if a not in ("--json", "-h", "--help")]
    if unknown:
        print(f"navig-media: unknown argument {unknown[0]!r} (try --json)", file=sys.stderr)
        return 2
    if "-h" in args or "--help" in args:
        print(
            "usage: navig-media [--help] [--json]\n"
            "\n"
            "Report what of the NAVIG media suite is installed: package, version,\n"
            "and the command each component answers to (standalone and in navig).\n"
            "Exit code 1 when a component is missing; --json for scripts."
        )
        return 0

    components = scan()
    installed = [c for c in components if c.installed]
    missing = [c for c in components if not c.installed]

    if as_json:
        print(json.dumps({"suite": [asdict(c) for c in components]}, indent=2))
        return 0 if not missing else 1

    # House style: a Rich table, semantic colour, one wrappable column, next-step nudge.
    from rich.console import Console
    from rich.table import Table

    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("Package", no_wrap=True)
    table.add_column("Does", no_wrap=True)
    table.add_column("Installed", no_wrap=True)
    table.add_column("Use it", style="dim")  # the free column
    for c in components:
        if c.standalone in ("navig-pipeline", "navig-download", "navig-social"):
            use = f"{c.standalone}  ·  {c.navig}"
        else:
            use = c.navig
        state = f"[green]● {c.installed}[/green]" if c.installed else "[red]✗ missing[/red]"
        table.add_row(c.package, c.does, state, use)

    console = Console()
    console.print(table)
    if missing:
        console.print(
            f"[yellow]{len(installed)}/{len(components)} installed[/yellow] · "
            "complete the suite: pip install navig-media"
        )
        return 1
    console.print(
        f"[green]{len(installed)}/{len(components)} installed[/green] · "
        "studio ready — pick a component above, or open NAVIG OS → Media Studio"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
