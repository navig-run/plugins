"""``navig text lyrics`` — write lyrics that sit on a beat.

  navig text lyrics scaffold beat.mp3 --lang ru,en,fr -o draft.md   # empty text cut to the bar map
  navig text lyrics fit draft.md --bpm 88                            # syllables per line vs the bar
  navig text lyrics fit --all lyrics/ --report FIT.md --beats beats/ # every text, nearest beat
  navig text lyrics words "капюшон" --bank lyrics/dictionary          # find a word-bank entry
  navig text lyrics rhyme района --in lyrics/                        # rhymes already in your texts
  navig text lyrics sheet draft.md --beat beat.mp3 --lang fr --start-bar 25 -o demo.yaml

No AI, no network: everything reads your files. The bar map comes from the beat's JSON
sidecar (``measured.bars``, written by ``navig audio beat gen``); navig-audio is used only
to measure a beat that has none. ``navig audio beat fit`` is an alias for ``fit`` here.
"""

from __future__ import annotations

import json as _json
import os
import statistics as _st
from pathlib import Path
from typing import Any, Optional

import typer
from navig_sdk import console as ch
from navig_sdk.host import command_name

from navig_text._typed_path import resolve_user_path

CMD = command_name("text", standalone="navig-text")

lyrics_app = typer.Typer(
    name="lyrics",
    help="🎤 Lyrics on a beat — fit, scaffold, word bank, rhymes, demo sheets.",
    no_args_is_help=True,
)


def _bars_for(beat: Path, bpm: Optional[float]) -> tuple[Optional[dict[str, Any]], Optional[float]]:
    """(bar map, bpm) for a beat: its sidecar first, then a measurement if navig-audio is here."""
    from navig_text.lyrics.scaffold import read_bar_map, sidecar_bpm

    bpm = bpm or sidecar_bpm(beat)
    bars = read_bar_map(beat)
    if bars is not None:
        return bars, bpm or float(bars.get("bpm") or 0) or None
    if beat.suffix.lower() == ".json":
        return None, bpm
    try:
        from navig_audio.beats.bars import bar_map
    except ImportError:
        return None, bpm
    if not bpm:
        return None, None
    with ch.create_spinner("Measuring the beat bar by bar…"):
        return bar_map(beat, bpm).to_dict(), bpm


def _strip_rows(bars: dict[str, Any], per_row: int = 16) -> list[str]:
    digits = str(bars.get("strip") or "")
    bar_s, phase = float(bars["bar_s"]), float(bars.get("phase_s", 0.0))
    rows = []
    for i in range(0, len(digits), per_row):
        chunk = digits[i:i + per_row]
        at = phase + i * bar_s
        m, s = divmod(at, 60)
        rows.append(f"bar {i + 1:>3} @ {int(m)}:{s:04.1f}  " + " ".join(chunk[j:j + 4] for j in range(0, len(chunk), 4)))
    return rows


@lyrics_app.command("fit")
def lyrics_fit(
    lyrics: Optional[list[Path]] = typer.Argument(None, help="Lyric files (beat-draft format or plain lyric)."),
    all_dirs: Optional[list[Path]] = typer.Option(None, "--all", help="Fit every .md under this folder (repeatable)."),
    bpm: Optional[float] = typer.Option(None, "--bpm", help="Tempo one line = one bar is counted at."),
    beat: Optional[Path] = typer.Option(None, "--beat", help="A beat (or its .json sidecar): take its tempo and print its bar map."),
    bars_per_line: int = typer.Option(1, "--bars-per-line", min=1, max=4, help="Bars one lyric line spans (2 on a fast beat)."),
    fast: bool = typer.Option(False, "--fast", help="Allow a denser line (fast technique delivery)."),
    report: Optional[Path] = typer.Option(None, "--report", help="Write a markdown report (one row per lyric + the edits)."),
    beats_dirs: Optional[list[Path]] = typer.Option(None, "--beats", help="Beat folder(s): suggest the nearest rendered beat for each text (repeatable)."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable result."),
) -> None:
    """📏 Does a lyric sit on the beat? Syllables per line against the time one bar gives."""
    from navig_text.lyrics.fit import fit, library_beats, nearest_beat, syllable_range, tempo_band

    library = library_beats([resolve_user_path(d) for d in beats_dirs]) if beats_dirs else []

    def _nearest(r) -> str:
        hit = nearest_beat(r.natural_bpm, library)
        return f"{hit[0]} ({hit[1]:g}) — {hit[2]}" if hit else "—"

    files: list[Path] = [resolve_user_path(f) for f in (lyrics or [])]
    for d in all_dirs or []:
        root = resolve_user_path(d)
        files += sorted(p for p in root.rglob("*.md") if p.name.lower() not in ("readme.md", "index.md", "fit.md"))
    if not files:
        ch.error("No lyric files given", "Pass files, or --all <folder>.")
        raise typer.Exit(2)

    bars = None
    if beat is not None:
        from navig_text.lyrics.scaffold import ScaffoldError

        try:
            bars, bpm = _bars_for(resolve_user_path(beat), bpm)
        except ScaffoldError as exc:
            ch.error("Cannot read this beat's bar map", str(exc))
            raise typer.Exit(2) from exc
        if bars is None:
            ch.warning("No bar map for this beat",
                       "It has no measured sidecar and navig-audio is not installed (or no --bpm) — "
                       "counting without the cue sheet.")

    reports = []
    for f in files:
        try:
            r = fit(f, bpm=bpm, bars_per_line=bars_per_line, fast=fast)
        except OSError as exc:
            ch.warning(f"skipped {f.name}", str(exc))
            continue
        if r.sections:
            reports.append(r)

    if as_json:
        payload: dict[str, Any] = {"bpm": bpm, "files": [r.to_dict() for r in reports]}
        if bars is not None:
            payload["bars"] = bars
        ch.raw_print(_json.dumps(payload, indent=2, ensure_ascii=False))
        return

    if bars is not None:
        ch.raw_print("\n".join(_strip_rows(bars)))
    if len(reports) == 1 and report is None:
        r = reports[0]
        table = ch.create_table(columns=[
            {"name": "lang"}, {"name": "section", "style": "cyan"}, {"name": "lines", "justify": "right"},
            {"name": "min·med·max", "justify": "right"}, {"name": "range", "justify": "right"},
            {"name": "off", "justify": "right"},
        ])
        for sf in r.sections:
            c = sf.section.counts
            table.add_row(sf.section.lang, sf.section.name[:40], str(len(c)),
                          f"{min(c)}·{_st.median(c):g}·{max(c)}", f"{sf.lo}–{sf.hi}",
                          str(len(sf.out)) if sf.section.kind == "verse" else "—")
        ch.print_table(table)
        for sf in r.sections:
            if sf.section.kind == "verse":
                for e in sf.edits():
                    ch.dim(f"  [{sf.section.lang} {sf.section.name[:24]}] {e}")
        ch.success(f"{r.out_count} verse line(s) off the bar" if r.out_count else "Every verse line sits in its bar",
                   f"natural tempo ≈ {r.natural_bpm:.0f} bpm — {tempo_band(r.natural_bpm)}"
                   + (f" · nearest beat: {_nearest(r)}" if library else ""))
        return

    if report is not None:
        report = resolve_user_path(report)
        if bpm:
            lo, hi = syllable_range(bpm, bars_per_line=bars_per_line, fast=fast)
            basis = f"Counted at {bpm:g} bpm → {lo}–{hi} syllables per line."
        else:
            basis = ("Each text is counted at its own natural tempo — the tempo at which its median "
                     "verse line fills one bar.")
        lines = [
            "# Lyric fit report",
            "",
            f"One line = {bars_per_line} bar(s). {basis}",
            "",
            "How to change a text so it sits on a beat: `docs/methods/fit-lyrics-to-a-beat.md`.",
            "",
            "| File | Lang | Lines | Median syll. | Natural tempo | Suits | Nearest beat | Lines off |",
            "|---|---|---|---|---|---|---|---|",
        ]
        base = report.parent
        for r in reports:
            p = Path(r.path)
            try:
                rel = Path(os.path.relpath(p, base)).as_posix()
            except ValueError:
                rel = p.as_posix()
            n = sum(len(sf.section.lines) for sf in r.sections)
            lines.append(f"| [`{p.name}`]({rel}) | {', '.join(r.languages)} | {n} | {r.median:g} | "
                         f"{r.natural_bpm:.0f} | {tempo_band(r.natural_bpm)} | {_nearest(r)} | {r.out_count} |")
        lines += ["", "## Edits, file by file", ""]
        for r in reports:
            edits = [e for sf in r.sections if sf.section.kind == "verse" for e in sf.edits()]
            if not edits:
                continue
            lines += [f"### {Path(r.path).name}", ""]
            lines += [f"- {e}" for e in edits[:40]]
            if len(edits) > 40:
                lines.append(f"- … and {len(edits) - 40} more — run `{CMD} lyrics fit` on this file alone")
            lines.append("")
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("\n".join(lines), encoding="utf-8")

    table = ch.create_table(columns=[
        {"name": "file", "style": "cyan"}, {"name": "lang"}, {"name": "median", "justify": "right"},
        {"name": "tempo", "justify": "right"}, {"name": "off", "justify": "right"},
    ])
    for r in reports[:60]:
        table.add_row(Path(r.path).name[:48], ",".join(r.languages), f"{r.median:g}",
                      f"{r.natural_bpm:.0f}", str(r.out_count))
    ch.print_table(table)
    ch.success(f"Fitted {len(reports)} lyric file(s)" + (f" → {report}" if report else ""))


@lyrics_app.command("scaffold")
def lyrics_scaffold(
    beat: Path = typer.Argument(..., help="A beat (its .json sidecar is read) or a bar-map JSON."),
    langs: str = typer.Option("ru,en,fr", "--lang", "-l", help="Languages, comma-separated (ru, en, fr)."),
    bpm: Optional[float] = typer.Option(None, "--bpm", help="Tempo lines are counted at (default: the render's target)."),
    bars_per_line: int = typer.Option(1, "--bars-per-line", min=1, max=4, help="Bars one lyric line spans (2 on a fast beat)."),
    fast: bool = typer.Option(False, "--fast", help="Allow a denser line (fast technique delivery)."),
    title: Optional[str] = typer.Option(None, "--title", "-t", help="The text's title (default: the beat's name)."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Write the skeleton here (default: print it)."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing --out file."),
) -> None:
    """🧱 An empty lyric cut to the beat's measured bar map — one slot per line."""
    from navig_text.lyrics.scaffold import ScaffoldError, plan, render

    src = resolve_user_path(beat)
    try:
        bars, bpm = _bars_for(src, bpm)
    except ScaffoldError as exc:
        ch.error("Cannot scaffold this beat", str(exc))
        raise typer.Exit(2) from exc
    if bars is None:
        ch.error("No bar map for this beat",
                 "Measure it first: navig audio beat analyse <beat> --bars --json > map.json "
                 "(then pass map.json), or install navig-audio and pass --bpm.")
        raise typer.Exit(2)
    if not bpm:
        ch.error("No tempo", "Pass --bpm (the sidecar has no target tempo).")
        raise typer.Exit(2)
    lang_list = [x.strip().lower() for x in langs.split(",") if x.strip()]
    try:
        slots = plan(bars, bars_per_line=bars_per_line)
        text = render(slots, bars, langs=lang_list, title=title or src.stem, beat=src.name,
                      bpm=bpm, bars_per_line=bars_per_line, fast=fast)
    except ScaffoldError as exc:
        ch.error("Cannot scaffold this beat", str(exc))
        raise typer.Exit(2) from exc
    if out is None:
        ch.raw_print(text)
        return
    dst = resolve_user_path(out)
    if dst.exists() and not force:
        ch.error(f"{dst.name} already exists", "Pass --force to overwrite, or pick another --out.")
        raise typer.Exit(2)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text, encoding="utf-8")
    lines = sum(s.lines for s in slots)
    ch.success(f"Scaffold → {dst}", f"{len(slots)} sections · {lines} line slots per language · "
               f"then: {CMD} lyrics fit {dst.name} --bpm {bpm:g}")


@lyrics_app.command("sheet")
def lyrics_sheet(
    lyrics: Path = typer.Argument(..., help="A beat-draft text with a hook and a voices table."),
    beat: Path = typer.Option(..., "--beat", help="The beat the demo plays under."),
    lang: str = typer.Option(..., "--lang", "-l", help="Which language's hook (ru, en, fr)."),
    start_bar: Optional[int] = typer.Option(None, "--start-bar", help="Bar the hook's first line lands on."),
    start_block: Optional[str] = typer.Option(None, "--start-block", help="…or a measured block: drop, full:2, build|full:1."),
    bars_per_line: int = typer.Option(1, "--bars-per-line", min=1, max=4, help="Bars one hook line spans."),
    bpm: Optional[float] = typer.Option(None, "--bpm", help="Bar tempo (default: the render's target)."),
    voice: Optional[str] = typer.Option(None, "--voice", help="Voice for hook lines the table does not cover."),
    length: float = typer.Option(30.0, "--length", help="Demo length in seconds."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Write the sheet here (default: print it)."),
) -> None:
    """🗒 A text's hook + its voices table → a sheet for `navig audio beat demo`."""
    from navig_text.lyrics.scaffold import sidecar_bpm
    from navig_text.lyrics.sheet import SheetError, build_sheet, dump

    src = resolve_user_path(lyrics)
    beat_p = resolve_user_path(beat)
    # The sheet keeps the path as given: resolving would follow a junction (a media folder
    # linked into a space) and pin the sheet to wherever it happens to point today. A
    # relative path is anchored where it was typed — made absolute, never resolved.
    typed = beat.expanduser()
    if not typed.is_absolute():
        origin = os.environ.get("NAVIG_INVOCATION_CWD")
        base = origin if origin and os.path.isdir(origin) else os.getcwd()
        typed = Path(os.path.normpath(os.path.join(base, typed)))
    try:
        text = src.read_text(encoding="utf-8")
        sheet = build_sheet(text, lang, typed.as_posix(), start_bar=start_bar, start_block=start_block,
                            bars_per_line=bars_per_line, bpm=bpm or sidecar_bpm(beat_p),
                            default_voice=voice, length_s=length)
    except (OSError, SheetError) as exc:
        ch.error("Cannot build the demo sheet", str(exc))
        raise typer.Exit(2) from exc
    body = dump(sheet, f"{beat_p.stem} ({lang.lower()})")
    if out is None:
        ch.raw_print(body)
        return
    dst = resolve_user_path(out)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(body, encoding="utf-8")
    ch.success(f"Demo sheet → {dst}", f"{len(sheet['lines'])} hook lines · then: "
               f"navig audio beat demo {beat_p.name} --sheet {dst.name} --voices <voices.yaml>")


@lyrics_app.command("words")
def lyrics_words(
    query: str = typer.Argument(..., help="A word or part of one."),
    bank: list[Path] = typer.Option(..., "--bank", "-b", help="Word-bank folder(s) or files (repeatable)."),
    limit: int = typer.Option(30, "--limit", "-n", min=1, max=500),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable result."),
) -> None:
    """🔎 Word-bank entries that mention a term — title hits first."""
    from navig_text.lyrics.bank import search

    dirs = [resolve_user_path(b) for b in bank]
    hits = search(dirs, query, limit=limit)
    if as_json:
        ch.raw_print(_json.dumps([{"path": str(h.path), "title": h.title, "snippet": h.snippet} for h in hits],
                                 ensure_ascii=False, indent=2))
        return
    if not hits:
        ch.info(f"No entry mentions {query!r}", "Try a shorter stem (капюшон → капюш).")
        return
    table = ch.create_table(columns=[{"name": "entry", "style": "cyan"}, {"name": "line"}, {"name": "file"}])
    for h in hits:
        table.add_row(h.title[:40], h.snippet[:70], h.path.name[:40])
    ch.print_table(table)


@lyrics_app.command("rhyme")
def lyrics_rhyme(
    word: str = typer.Argument(..., help="The word to rhyme."),
    within: list[Path] = typer.Option(..., "--in", help="Folder(s) whose words are candidates: a word bank, your texts (repeatable)."),
    lang: Optional[str] = typer.Option(None, "--lang", "-l", help="ru, en or fr (default: detected)."),
    limit: int = typer.Option(40, "--limit", "-n", min=1, max=500),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable result."),
) -> None:
    """🎯 Rhymes for a word from the words you already use — spelling-based, strong first."""
    from navig_text.lyrics.bank import rhymes, vocabulary

    vocab = vocabulary([resolve_user_path(d) for d in within])
    found = rhymes(word, vocab, lang=lang, limit=limit)
    if as_json:
        ch.raw_print(_json.dumps([{"word": w, "match": k, "uses": n} for w, k, n in found], ensure_ascii=False, indent=2))
        return
    if not found:
        ch.info(f"No rhyme for {word!r} in {len(vocab)} words", "Point --in at more texts, or try a related form.")
        return
    strong = [f"{w} ({n})" for w, k, n in found if k == "strong"]
    weak = [f"{w} ({n})" for w, k, n in found if k == "weak"]
    if strong:
        ch.raw_print("strong: " + ", ".join(strong))
    if weak:
        ch.raw_print("weak:   " + ", ".join(weak))
    ch.dim(f"  from {len(vocab)} words · (n) = times used · spelling match, trust your ear")
