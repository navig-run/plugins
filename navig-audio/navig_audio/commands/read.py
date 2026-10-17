"""``navig audio read`` — read a text aloud, like a book.

  navig audio read "Once upon a time…"              # a short text
  navig audio read --file chapter-3.txt             # a file (any code page)
  type story.txt | navig audio read -               # from stdin
  navig audio read --file book.md --dry-run         # what it would cost — spends nothing
  navig audio read --file book.md --voice Marcus --speed 0.85

The same engine reads you to sleep from Telegram (``/read``, or a .txt sent with the caption
``/read``); this is its terminal door. It writes numbered parts of about ten minutes
(``read-<id>-part-01-of-04.mp3`` …) into ``<audio dir>/read/<id>/`` and prints each as it is
ready. Every chunk is cached, so running it again — or after a failure — costs nothing for
what was already made.

Mounted on ``audio_app`` by import side-effect from ``audio.py``, like ``spoken``.
"""

from __future__ import annotations

import asyncio
import json as _json
import sys
from pathlib import Path
from typing import Optional

import typer

from navig_audio._typed_path import resolve_user_path
from navig_audio.commands.audio import audio_app
from navig_sdk import console as ch


def _usage(message: str, hint: str = "") -> None:
    """A problem with the request itself — exit 2, per the plugin's exit-code contract."""
    ch.error(message, hint)
    raise typer.Exit(2)


def _read_input(text: Optional[str], file: Optional[Path]) -> str:
    from navig_audio.reader import decode_text_file

    if file is not None and text:
        _usage("Give the text OR --file, not both.")
    if file is not None:
        path = resolve_user_path(file)
        if not path.is_file():
            _usage(f"No such file: {path}")
        return decode_text_file(path.read_bytes())
    if text == "-":
        data = sys.stdin.buffer.read()
        return decode_text_file(data)
    if text:
        return text
    _usage(
        "Nothing to read.",
        'Pass the text, --file <path>, or "-" to read stdin.  Try: navig audio read --file chapter.txt --dry-run',
    )
    return ""  # unreachable; keeps the type checker honest


@audio_app.command("read")
def audio_read(
    text: Optional[str] = typer.Argument(None, help='The text to read, or "-" for stdin.'),
    file: Optional[Path] = typer.Option(None, "--file", "-f", help="Read a .txt / .md file instead."),
    voice: Optional[str] = typer.Option(
        None, "--voice", help="A voice id, or the NAME of one of your voices (see `navig audio voices`)."
    ),
    lang: Optional[str] = typer.Option(None, "--lang", help="en | ru | fr. Default: detected from the text."),
    speed: Optional[float] = typer.Option(
        None, "--speed", help="0.7-1.2. Default 0.9 — unhurried, for the pillow."
    ),
    part_minutes: Optional[float] = typer.Option(
        None, "--part-min", help="Target length of each file in minutes. Default 10."
    ),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Directory to write into."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show length, parts and credits. Spends nothing."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Go ahead even if it costs more than 15,000 credits."),
    as_json: bool = typer.Option(False, "--json", help="Emit the plan / result as JSON."),
) -> None:
    """📖 Read a text aloud, like a book — numbered ten-minute parts, quiet and unhurried."""
    from navig_audio import reader, reading_job
    from navig_audio.reader import CONFIRM_CREDITS, ReadError

    raw = _read_input(text, file)
    try:
        options = reader.options_from_config(
            voice=voice, lang=lang.lower() if lang else None, speed=speed, part_minutes=part_minutes
        )
        plan = reader.plan_reading(raw, options)
    except ReadError as exc:
        _usage(str(exc))
        return

    spend = reader.estimate_spend(plan, options, reading_job._cheap_voice_id(options))
    quote = {**plan.to_dict(), "spend": spend, "confirm_over": CONFIRM_CREDITS}

    if dry_run:
        if as_json:
            ch.raw_print(_json.dumps(quote, indent=2))
        else:
            _print_quote(plan, spend)
            ch.info("Dry run — nothing was generated or charged.")
        return

    remaining = asyncio.run(reading_job._remaining_credits())
    if remaining is not None and spend > remaining:
        ch.error(
            f"This reading needs about {spend:,} credits and the account has {remaining:,} left.",
            "Read a shorter text, or wait for the monthly reset. Chunks already made are cached and free.",
        )
        raise typer.Exit(1)
    if spend > CONFIRM_CREDITS and not yes:
        _print_quote(plan, spend)
        ch.error(
            f"That is about {spend:,} credits — more than {CONFIRM_CREDITS:,}.",
            "Add --yes to go ahead, or --dry-run to look without spending.",
        )
        raise typer.Exit(2)

    if not as_json:
        _print_quote(plan, spend)
    out_dir = (
        resolve_user_path(out) if out else _default_out_dir()
    ) / plan.slug

    def progress(message: str) -> None:
        if not as_json:
            ch.info(message)

    async def deliver(part) -> bool:
        if not as_json:
            ch.success(f"part {part.caption}", str(part.path))
        return True

    try:
        summary = asyncio.run(
            reader.read_aloud(plan, options, out_dir, deliver=deliver, progress=progress)
        )
    except ReadError as exc:
        ch.error("Could not start reading", str(exc))
        raise typer.Exit(1) from exc

    if as_json:
        ch.raw_print(_json.dumps({**summary.to_dict(), "credits_spent": summary.credits_spent}, indent=2))
    else:
        for warning in summary.warnings:
            ch.warning(warning)
        if summary.error:
            ch.error("Reading stopped early", summary.error)
        else:
            ch.success(
                f"Read {len(summary.parts)} part(s) · {summary.credits_spent:,} credits spent",
                f"Saved under {out_dir}",
            )
    if summary.error or not summary.parts:
        raise typer.Exit(1)


def _default_out_dir() -> Path:
    from navig_generate.tools.audio_generation import AudioGenerationConfig

    return Path(AudioGenerationConfig.from_env().output_dir).expanduser() / "read"


def _print_quote(plan, spend: int) -> None:
    table = ch.create_table(
        columns=[
            {"name": "what", "style": "cyan"},
            {"name": "value"},
        ],
    )
    table.add_row("length", f"{plan.chars:,} characters · about {plan.minutes:.0f} min")
    table.add_row(
        "files", f"{plan.part_count} part(s) of about {round(plan.minutes / plan.part_count)} min"
    )
    table.add_row("language", plan.lang or "auto (the model decides)")
    table.add_row("pace", f"{plan.speed:g}x")
    table.add_row("credits", f"{spend:,}" + (" (cached — free)" if spend == 0 else f" of {plan.credits:,} at full price"))
    ch.print_table(table)
