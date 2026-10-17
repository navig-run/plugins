"""``navig audio songs`` — one folder per song, a card in each, a catalog built from the cards.

  navig audio songs card <song-folder> --id snm-016 --title Страницы --status released --release Холодец --year 2016 --track 1 --lang ru
  navig audio songs card <song-folder>                 refresh the facts block (lyrics, master, takes …)
  navig audio songs catalog <songs-root> --md <songs-root>/CATALOG.md --csv <state>/songs.csv
  navig audio songs catalog <songs-root> --refresh     re-read every folder first
  navig audio songs check <songs-root>                 duplicate ids, unknown status, recorded with no audio

Mounted on ``audio_app`` by import side-effect from ``audio.py``, like ``beat`` and ``master``.
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Optional

import typer

from navig_sdk import console as ch
from navig_audio.commands.audio import audio_app

songs_app = typer.Typer(help="🎼 Song folders: a card per song, a catalog generated from the cards.",
                        no_args_is_help=True)
audio_app.add_typer(songs_app, name="songs")


@songs_app.command("card")
def songs_card(
    folder: Path = typer.Argument(..., exists=True, file_okay=False, help="The song's folder."),
    song_id: Optional[str] = typer.Option(None, "--id", help="Catalog id (e.g. snm-016)."),
    title: Optional[str] = typer.Option(None, "--title"),
    status: Optional[str] = typer.Option(None, "--status",
                                         help="idea | draft | lyrics | demo | recorded | mixed | mastered | released"),
    release: Optional[str] = typer.Option(None, "--release", help="Album / single / EP it came out on."),
    year: Optional[str] = typer.Option(None, "--year"),
    track: Optional[int] = typer.Option(None, "--track", help="Track number on the release."),
    lang: Optional[str] = typer.Option(None, "--lang", help="ru · en · fr · ru/en …"),
    master: Optional[str] = typer.Option(None, "--master", help="File name of the release version (else guessed)."),
    soundcloud: Optional[str] = typer.Option(None, "--soundcloud"),
    session: Optional[str] = typer.Option(None, "--session", help="Where its DAW session lives (a path)."),
    beat: Optional[str] = typer.Option(None, "--beat", help="The beat it is on, when not copied into beat/."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the card; write nothing."),
):
    """🪪 Create or refresh a song card (README.md front matter + facts read from the folder)."""
    from navig_audio.songs.card import SongError, write_card

    updates = {"id": song_id, "title": title, "status": status, "release": release, "year": year,
               "track": track, "lang": lang, "master": master, "soundcloud": soundcloud,
               "session": session, "beat": beat}
    try:
        path, text = write_card(folder, updates, dry_run=dry_run)
    except SongError as exc:
        ch.error("Could not write the card", str(exc))
        raise typer.Exit(2) from exc
    if dry_run:
        ch.raw_print(text)
        return
    ch.success(f"card → {path}")


@songs_app.command("catalog")
def songs_catalog(
    root: Path = typer.Argument(..., exists=True, file_okay=False, help="Where the song folders are."),
    md: Optional[Path] = typer.Option(None, "--md", help="Markdown to write the catalog into (between markers)."),
    csv_out: Optional[Path] = typer.Option(None, "--csv", help="CSV mirror, one row per song."),
    refresh: bool = typer.Option(False, "--refresh", help="Refresh every card's facts block first."),
    as_json: bool = typer.Option(False, "--json", help="Print the songs as JSON."),
):
    """📚 Build the catalog from every song card under ROOT."""
    from navig_audio.songs.card import scan, write_card
    from navig_audio.songs.catalog import problems, write_catalog, write_csv

    songs = scan(root)
    if refresh:
        for s in songs:
            write_card(s.folder)
        songs = scan(root)
    if as_json:
        ch.raw_print(_json.dumps([{**s.meta, "folder": s.folder.as_posix(), **s.facts.as_dict()} for s in songs],
                                 indent=2, ensure_ascii=False, default=str))
        return
    if md:
        write_catalog(songs, md)
        ch.success(f"catalog → {md}")
    if csv_out:
        write_csv(songs, csv_out, root)
        ch.success(f"csv → {csv_out}")
    issues = problems(songs)
    ch.info(f"{len(songs)} song(s) · {len(issues)} issue(s)" + ("" if md or csv_out else " — pass --md/--csv to write"))
    for i in issues[:20]:
        ch.dim(f"  {i}")


@songs_app.command("check")
def songs_check(
    root: Path = typer.Argument(..., exists=True, file_okay=False),
):
    """🩺 Duplicate ids, unknown status, a recorded song with no audio. Exit 1 when anything is wrong."""
    from navig_audio.songs.card import scan
    from navig_audio.songs.catalog import problems

    songs = scan(root)
    issues = problems(songs)
    for i in issues:
        ch.warning(i)
    ch.info(f"{len(songs)} song(s) · {len(issues)} issue(s)")
    if issues:
        raise typer.Exit(1)
