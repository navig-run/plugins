"""Episode compilation verbs, mounted onto ``navig audio``.

These live in their own module rather than in ``audio.py`` because they are a pipeline
rather than one-shot effects, but they are deliberately **not** a separate top-level
command: making an episode is audio generation, and splitting it across two verbs would
mean two places to look for the same key, the same cache and the same provider.

Reading order is the pipeline order:

    draft → plan → translate → render

Each command is a thin shell. Everything it actually does lives in
``navig_audio.podcast``, so the logic is testable without a terminal.
"""

from __future__ import annotations

import asyncio
import json as _json
from pathlib import Path
from typing import Optional

import typer

from navig.lazy_loader import lazy_import
from navig_audio.commands.audio import audio_app

ch = lazy_import("navig.console_helper")


def _fail_usage(message: str, hint: str = "") -> None:
    """A problem with the request itself — exit 2, per the plugin's exit-code contract."""
    ch.error(message, hint)
    raise typer.Exit(2)


def _load(scenario: Path):
    """Parse a scenario, turning a parse error into an actionable usage failure."""
    from navig_audio.podcast.scenario import ScenarioError, load

    try:
        return load(scenario)
    except ScenarioError as exc:
        _fail_usage(str(exc))
    return None  # unreachable; keeps the type checker honest


def _langs(value: str | None, fallback: str) -> list[str]:
    if not value:
        return [fallback]
    return [part.strip().lower() for part in value.split(",") if part.strip()]


def _sibling_for(scenario: Path, source_lang: str, target_lang: str) -> Path:
    """Where the translated twin of ``scenario`` would live.

    ``ep000.fr.md`` and ``en`` gives ``ep000.en.md``. The convention is a language
    suffix on the stem, so a file's language is visible without opening it.
    """
    stem = scenario.stem
    head, _, tail = stem.rpartition(".")
    if head and tail.lower() == source_lang.lower():
        stem = head
    return scenario.with_name(f"{stem}.{target_lang}.md")


# ── draft ──────────────────────────────────────────────────────────────────────


@audio_app.command("draft")
def audio_draft(
    source: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                                  help="Rough notes / outline to convert (.md, .txt)."),
    out: Optional[Path] = typer.Option(None, "--out", "-o",
                                       help="Where to write the scenario (default: beside the source)."),
    lang: Optional[str] = typer.Option(None, "--lang", "-l",
                                       help="Language of the notes, e.g. fr. Detected if omitted."),
    title: Optional[str] = typer.Option(None, "--title", "-t", help="Episode title."),
    episode: Optional[int] = typer.Option(None, "--episode", "-e", help="Episode number."),
    speaker: str = typer.Option("HOST", "--speaker", "-s", help="Default speaker name (ALL CAPS)."),
) -> None:
    """📝 Turn rough notes into a strict scenario you can review before rendering.

    Sorts the spoken words from the production notes and writes a file in the format
    `render` accepts. Read it before rendering — this is the cheap place to catch a
    misjudged line, because after rendering you have already paid to hear it.

        navig audio draft Script.md --lang fr --episode 0
    """
    from navig_audio.podcast.draft import DraftError, draft_file

    try:
        with ch.create_spinner("Drafting the scenario…"):
            parsed, written = draft_file(
                source, out, lang=lang, title=title,
                episode_number=episode, default_speaker=speaker,
            )
    except DraftError as exc:
        ch.error("Could not draft a scenario", str(exc))
        raise typer.Exit(1) from exc

    ch.success(
        f"Wrote {written}",
        f"{len(parsed.tracks)} track(s) · {parsed.billable_chars:,} spoken characters",
    )
    unvoiced = parsed.unvoiced_speakers()
    if unvoiced:
        ch.dim(
            f"Next: read it, then add a voice id for {', '.join(unvoiced)} under `voices:` "
            f"(list yours with `navig audio voices`)"
        )


# ── plan ───────────────────────────────────────────────────────────────────────


@audio_app.command("plan")
def audio_plan(
    scenario: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                                    help="Scenario file to cost."),
    lang: Optional[str] = typer.Option(None, "--lang", "-l",
                                       help="Languages to cost, e.g. fr,en. Default: the scenario's own."),
    as_json: bool = typer.Option(False, "--json", help="Emit the estimate as JSON."),
) -> None:
    """🧮 What this episode will cost, before it costs it. Spends nothing.

    Counts only what will actually be sent — production notes and cue lines are already
    excluded — and checks the total against the credits left on your plan.

        navig audio plan ep000.fr.md --lang fr,en
    """
    from navig_audio.podcast.cost import estimate, parse_balance, shortfall_message
    from navig_audio.podcast.voices import account

    parsed = _load(scenario)
    projection = estimate(parsed, _langs(lang, parsed.lang))

    balance = None
    try:
        balance = parse_balance(asyncio.run(account()))
    except Exception:  # noqa: BLE001 - an estimate is still useful with no network
        pass

    if as_json:
        payload = projection.to_dict()
        payload["balance"] = balance.to_dict() if balance else None
        ch.raw_print(_json.dumps(payload, indent=2, ensure_ascii=False))
        return

    table = ch.create_table(
        columns=[
            {"name": "#", "style": "dim", "justify": "right"},
            {"name": "track", "style": "cyan"},
            {"name": "chars", "justify": "right"},
            {"name": "requests", "justify": "right", "style": "dim"},
            {"name": "voices", "style": "dim"},
        ],
    )
    for track in projection.tracks:
        table.add_row(
            f"{track.number:02d}", track.title, f"{track.chars:,}",
            str(track.chunks), ", ".join(track.speakers) or "—",
        )
    ch.print_table(table)

    langs = ", ".join(projection.languages)
    ch.info(
        f"{projection.chars_per_language:,} characters per language · "
        f"~{projection.estimated_minutes:.0f} min of audio · {langs}"
    )
    ch.info(
        f"Estimated cost: {projection.total_credits:,} credits "
        f"across {projection.total_requests} request(s)"
    )

    if balance is not None:
        ch.dim(f"Plan {balance.tier} · {balance.remaining:,} credits remaining")
        warning = shortfall_message(projection, balance)
        if warning:
            ch.warning("This will not fit in the remaining balance", warning)
        else:
            ch.success("The remaining balance covers this render")

    unvoiced = parsed.unvoiced_speakers()
    if unvoiced:
        ch.warning(
            f"No voice id yet for: {', '.join(unvoiced)}",
            "Planning works without one; rendering does not. `navig audio voices` lists yours.",
        )


# ── translate ──────────────────────────────────────────────────────────────────


@audio_app.command("translate")
def audio_translate(
    scenario: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                                    help="Scenario file to translate."),
    to: str = typer.Option(..., "--to", "-t", help="Target language code, e.g. en."),
    out: Optional[Path] = typer.Option(None, "--out", "-o",
                                       help="Output path (default: <name>.<lang>.md beside the source)."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing translation."),
) -> None:
    """🌍 Translate the spoken lines, keeping the album structure identical.

    Only speech is sent to the model. Track numbers, speaker tags, music and SFX cues and
    the voice map are carried over mechanically, so the translated episode is guaranteed
    to have the same tracks in the same order.

        navig audio translate ep000.fr.md --to en
    """
    from navig_audio.podcast.scenario import dump
    from navig_audio.podcast.translate import TranslationError, translate_episode

    parsed = _load(scenario)
    target = to.strip().lower()
    destination = out or _sibling_for(scenario, parsed.lang, target)

    if destination.exists() and not force:
        _fail_usage(
            f"{destination.name} already exists",
            "Pass --force to overwrite it, or --out to write somewhere else.",
        )

    try:
        with ch.create_spinner(f"Translating {len(parsed.tracks)} track(s) to {target}…"):
            translated = translate_episode(parsed, target)
    except TranslationError as exc:
        ch.error("Translation failed", str(exc))
        raise typer.Exit(1) from exc

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(dump(translated), encoding="utf-8")
    ch.success(
        f"Wrote {destination}",
        f"{len(translated.tracks)} track(s) · {translated.billable_chars:,} spoken characters",
    )
    ch.dim("Read it before rendering — a translation is a draft too.")


# ── render ─────────────────────────────────────────────────────────────────────


@audio_app.command("render")
def audio_render(
    scenario: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                                    help="Scenario file to render."),
    lang: Optional[str] = typer.Option(None, "--lang", "-l",
                                       help="Languages to render, e.g. fr,en. Needs a translated file per language."),
    track: Optional[str] = typer.Option(None, "--track",
                                        help="Render only these track numbers, e.g. 3 or 3,5."),
    out: Optional[Path] = typer.Option(None, "--out", "-o",
                                       help="Output directory (default: ./episodes)."),
    no_master: bool = typer.Option(False, "--no-master", help="Skip the stitched master file."),
    as_json: bool = typer.Option(False, "--json", help="Emit the result as JSON."),
) -> None:
    """🎧 Build the episode — one mp3 per track, plus subtitles, transcript and a master.

    Every clip is cached by content, so re-running after an edit only pays for what
    changed, and `--track` re-does a single segment without re-buying the episode.

        navig audio render ep000.fr.md --lang fr,en
        navig audio render ep000.fr.md --track 03
    """
    from navig_audio.podcast.render import RenderError, render_episode
    from navig_audio.podcast.scenario import ScenarioError, load

    parsed = _load(scenario)
    targets = _langs(lang, parsed.lang)

    tracks: list[int] | None = None
    if track:
        try:
            tracks = [int(part.strip()) for part in track.split(",") if part.strip()]
        except ValueError:
            _fail_usage(f"--track expects numbers, got {track!r}", "For example: --track 3,5")

    # Resolve every scenario up front. Discovering a missing translation after the first
    # language has already been billed would be an expensive way to learn about a typo.
    episodes = []
    for target in targets:
        if target == parsed.lang:
            episodes.append(parsed)
            continue
        sibling = _sibling_for(scenario, parsed.lang, target)
        if not sibling.exists():
            _fail_usage(
                f"no scenario for {target!r} at {sibling.name}",
                f"Create it first:  navig audio translate {scenario.name} --to {target}",
            )
        try:
            episodes.append(load(sibling))
        except ScenarioError as exc:
            _fail_usage(str(exc))

    destination = (out or Path("episodes")) / parsed.dirname
    results = []
    try:
        for episode in episodes:
            ch.info(f"Rendering {episode.lang} — {episode.title}")
            results.append(
                asyncio.run(
                    render_episode(
                        episode, destination,
                        tracks=tracks, master=not no_master,
                        progress=ch.dim,
                    )
                )
            )
    except typer.Exit:
        # typer.Exit subclasses RuntimeError, so it would be swallowed by the handlers
        # below and turned into a "render failed" that never happened.
        raise
    except RenderError as exc:
        ch.error("Render failed", str(exc))
        raise typer.Exit(1) from exc
    except Exception as exc:  # provider / network / quota — surface it, do not swallow
        ch.error("Render failed", str(exc))
        raise typer.Exit(1) from exc

    if as_json:
        ch.raw_print(_json.dumps([r.to_dict() for r in results], indent=2, ensure_ascii=False))
        return

    table = ch.create_table(
        columns=[
            {"name": "lang", "style": "magenta"},
            {"name": "#", "style": "dim", "justify": "right"},
            {"name": "track", "style": "cyan"},
            {"name": "length", "justify": "right"},
            {"name": "clips", "style": "dim", "justify": "right"},
            {"name": "file"},
        ],
    )
    for result in results:
        for rendered in result.tracks:
            minutes, seconds = divmod(int(rendered.duration_s), 60)
            table.add_row(
                result.lang, f"{rendered.number:02d}", rendered.title,
                f"{minutes}:{seconds:02d}",
                f"{rendered.generated}+{rendered.cached}",
                rendered.path.name,
            )
    ch.print_table(table)

    spent = sum(r.credits_spent for r in results)
    reused = sum(t.cached for r in results for t in r.tracks)
    total_s = sum(r.duration_s for r in results)
    ch.success(
        f"Rendered {sum(len(r.tracks) for r in results)} track(s) · "
        f"{int(total_s // 60)} min {int(total_s % 60)} s",
        f"Saved under {destination}",
    )
    ch.info(f"Credits spent: ~{spent:,}" + (f"  ({reused} clip(s) reused free)" if reused else ""))
    for result in results:
        if result.master:
            ch.dim(f"{result.lang}: master → {result.master}")


# ── voices ─────────────────────────────────────────────────────────────────────


@audio_app.command("voices")
def audio_voices(
    mine: bool = typer.Option(False, "--mine", help="Only voices you created (clones)."),
    as_json: bool = typer.Option(False, "--json", help="Emit the list as JSON."),
) -> None:
    """🗣  List the voices this key can speak with — your clones first.

    The ids here are what go under `voices:` in a scenario's frontmatter.
    """
    from navig_audio.podcast.voices import list_voices

    try:
        found = asyncio.run(list_voices(mine_only=mine))
    except Exception as exc:  # noqa: BLE001 - key/network problems belong to the user
        ch.error("Could not list voices", str(exc))
        raise typer.Exit(1) from exc

    if not found:
        ch.warning(
            "No voices found" + (" that you created" if mine else ""),
            "Create one from your own recordings:  navig audio clone \"<name>\" <samples>",
        )
        raise typer.Exit(1)

    if as_json:
        ch.raw_print(_json.dumps([v.to_dict() for v in found], indent=2, ensure_ascii=False))
        return

    table = ch.create_table(
        columns=[
            {"name": "name", "style": "cyan"},
            {"name": "voice_id"},
            {"name": "kind", "style": "dim"},
        ],
    )
    for voice in found:
        table.add_row(
            f"[bold]{voice.name}[/bold]" if voice.is_mine else voice.name,
            voice.voice_id,
            "yours" if voice.is_mine else voice.category,
        )
    ch.print_table(table)
    ch.dim("Put a voice_id under `voices:` in your scenario frontmatter.")


# ── clone ──────────────────────────────────────────────────────────────────────


@audio_app.command("clone")
def audio_clone(
    name: str = typer.Argument(..., help="A name for the voice, e.g. \"Serio\"."),
    samples: list[Path] = typer.Argument(..., help="Audio samples, or a folder of them."),
    description: Optional[str] = typer.Option(None, "--description", "-d", help="Optional voice description."),
    keep_noise: bool = typer.Option(False, "--keep-noise",
                                    help="Do not let the provider clean the samples."),
    as_json: bool = typer.Option(False, "--json", help="Emit the result as JSON."),
) -> None:
    """👤 Clone a voice from your own recordings and print its id.

    A minute or two of clean, consistent speech is enough. Use one microphone, one room
    and no music — the clone copies the recording conditions along with the voice.

    Cloning needs a paid ElevenLabs plan; `navig audio check` reports whether yours has it.

        navig audio clone "Serio" ./samples
    """
    from navig_audio.podcast.voices import clone, collect_samples

    found = collect_samples(samples)
    if not found:
        _fail_usage(
            "no usable audio samples found",
            "Give audio files or a folder containing them (mp3, wav, m4a, flac, ogg).",
        )

    ch.info(f"Uploading {len(found)} sample(s): {', '.join(p.name for p in found[:4])}"
            + (" …" if len(found) > 4 else ""))
    try:
        with ch.create_spinner("Creating the voice clone…"):
            result = asyncio.run(
                clone(name, found, description=description, remove_background_noise=not keep_noise)
            )
    except Exception as exc:  # noqa: BLE001 - plan/quota errors are the user's to act on
        ch.error(
            "Could not create the voice clone",
            f"{exc}\nCloning requires a paid plan — check with `navig audio check`.",
        )
        raise typer.Exit(1) from exc

    voice_id = result.get("voice_id")
    if not voice_id:
        ch.error("The provider did not return a voice id", str(result))
        raise typer.Exit(1)

    if as_json:
        ch.raw_print(_json.dumps(result, indent=2, ensure_ascii=False))
        return

    ch.success(f"Created voice {name!r}", f"voice_id: {voice_id}")
    ch.dim("Add it to your scenario frontmatter:")
    ch.raw_print(f"\nvoices:\n  {name.upper().replace(' ', '_')}: \"{voice_id}\"\n")


# ── publish (planning only) ────────────────────────────────────────────────────


@audio_app.command("publish")
def audio_publish(
    episode_dir: Path = typer.Argument(..., exists=True, file_okay=False,
                                       help="A rendered episode directory (the one holding metadata.json)."),
    lang: Optional[str] = typer.Option(None, "--lang", "-l", help="Limit to these languages, e.g. fr,en."),
    as_json: bool = typer.Option(False, "--json", help="Emit the plan as JSON."),
) -> None:
    """📤 Show exactly what would be uploaded to NobiCast. Uploads nothing yet.

    The upload itself is not wired: NobiCast currently stores one audio file per episode
    and has no language column, so an album cannot land there without a schema change.
    This prints the intended keys and flags the blockers.
    """
    from navig_audio.podcast.publish import PublishError, blockers, plan_upload

    try:
        plan = plan_upload(episode_dir, languages=_langs(lang, "") if lang else None)
    except PublishError as exc:
        _fail_usage(str(exc))
        return

    if as_json:
        payload = plan.to_dict()
        payload["blockers"] = blockers()
        ch.raw_print(_json.dumps(payload, indent=2, ensure_ascii=False))
        return

    table = ch.create_table(
        columns=[
            {"name": "lang", "style": "magenta"},
            {"name": "#", "style": "dim", "justify": "right"},
            {"name": "r2 key", "style": "cyan"},
            {"name": "size", "justify": "right"},
        ],
    )
    for upload in plan.uploads:
        table.add_row(
            upload.lang, f"{upload.track_no:02d}", upload.r2_key,
            f"{upload.bytes / 1_048_576:.1f} MB",
        )
    ch.print_table(table)

    for warning in plan.warnings:
        ch.warning(warning)

    ch.info(f"{len(plan.uploads)} file(s) across {', '.join(plan.languages)}")
    ch.warning(
        "Upload is not wired yet — NobiCast needs these first:",
        "\n".join(f"• {item}" for item in blockers()),
    )
