"""``navig audio spoken`` — what the narrator says, not what the screen shows.

  navig audio spoken "1 из 5" --lang ru           # один из пяти
  navig audio spoken "Станция 13010" --lang ru    # тринадцать-ноль-десять
  navig audio spoken "266 cities" --lang ru --groups   # token → words, JSON

Mounted on ``audio_app`` by import side-effect from ``audio.py``, like ``beat``.
"""

from __future__ import annotations

import json as _json

import typer

from navig_sdk import console as ch
from navig_audio.commands.audio import audio_app


@audio_app.command("spoken")
def audio_spoken(
    text: str = typer.Argument(..., help="Display text — digits, coordinates, station numbers."),
    lang: str = typer.Option("ru", "--lang", help="en | ru | fr."),
    groups: bool = typer.Option(False, "--groups", help="Emit the token→words map instead of the flat line."),
) -> None:
    """🔢 Display text → the words a voice should actually speak (cases, ordinals, years)."""
    from navig_audio.speech import groups as group_words
    from navig_audio.speech import narration

    if groups:
        toks = text.replace("*", "").split(" ")
        out = group_words(toks, lang)
        ch.raw_print(_json.dumps(out, ensure_ascii=False, indent=2))
        return
    ch.raw_print(narration(text, lang))
