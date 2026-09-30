"""``navig pipeline`` — the content assembly line.

Chains the media plugin family into one flow: draft a post from a topic (or a
downloaded+transcribed clip), optionally voice it, and fan it out — each stage
provided by an independently installable plugin.

  navig pipeline status                                   what's wired right now
  navig pipeline run --topic "NAVIG v2 ships" --to x,telegram --dry-run
  navig pipeline run --topic "…" --to x,telegram --narrate     # + audio voiceover
  navig pipeline run --source clip.mp4 --to devto              # transcribe → post

Dry-run (default) drafts the real caption and previews the fan-out, but performs
no download / narration / publish. Pass --live to actually publish.
"""

from __future__ import annotations

import asyncio
import json as _json
from pathlib import Path
from typing import Optional

import typer

from navig_sdk import console as ch
from navig_sdk.host import command_name

# The command a user types here: `navig pipeline` inside navig, `navig-pipeline` on its own.
CMD = command_name("pipeline")

pipeline_app = typer.Typer(
    name="pipeline",
    help="🏭 The content assembly line — download → transcribe → script → narrate → fan-out.",
    no_args_is_help=True,
)

_STATUS_GLYPH = {
    "ran": "[green]● ran[/green]",
    "preview": "[cyan]◆ preview[/cyan]",
    "skipped": "[dim]○ skipped[/dim]",
    "failed": "[red]✗ failed[/red]",
}


def _validate_kind(kind: str) -> None:
    """Reject a typo'd --kind up front. navig-text safely defaults an unknown kind to
    'draft', but a silent default hides the mistake — surface it here."""
    try:
        from navig_text.generation import KINDS
    except ImportError:
        return  # navig-text absent → the script stage reports it; don't block here
    if kind not in KINDS:
        ch.error(f"Unknown --kind {kind!r}", f"Use one of: {', '.join(KINDS)}")
        raise typer.Exit(2)


@pipeline_app.command("status")
def pipeline_status(as_json: bool = typer.Option(False, "--json")) -> None:
    """Show which assembly-line stages are wired (their plugin is installed)."""
    from navig_pipeline.pipeline import STAGE_PLUGIN, detect_capabilities

    caps = detect_capabilities()
    if as_json:
        ch.raw_print(_json.dumps({"stages": caps, "providers": STAGE_PLUGIN}, indent=2))
        return

    table = ch.create_table(
        columns=[
            {"name": "stage", "style": "cyan"},
            {"name": "provider", "style": "dim"},
            {"name": "status"},
        ],
    )
    for stage, provider in STAGE_PLUGIN.items():
        ok = caps.get(stage, False)
        table.add_row(stage, provider, "[green]● wired[/green]" if ok else f"[yellow]○ install {provider}[/yellow]")
    ch.print_table(table)
    ready = sum(1 for v in caps.values() if v)
    ch.dim(f"{ready}/{len(caps)} stages wired · {CMD} run --topic \"…\" --to x,telegram --dry-run")


@pipeline_app.command("run")
def pipeline_run(
    topic: Optional[str] = typer.Option(None, "--topic", "-t", help="What to write about (text-first)."),
    source: Optional[str] = typer.Option(None, "--source", help="A media URL/file to transcribe first."),
    to: str = typer.Option("x,telegram", "--to", help="Comma-separated networks to fan out to."),
    kind: str = typer.Option("social", "--kind", "-k", help="Post style: social | caption | brief | article."),
    narrate: bool = typer.Option(False, "--narrate", help="Also generate an audio voiceover (navig-audio)."),
    campaign: Optional[str] = typer.Option(None, "--campaign", help="UTM campaign slug."),
    url: Optional[str] = typer.Option(None, "--url", help="Canonical hub URL to link (gets UTM)."),
    dry_run: bool = typer.Option(
        True, "--dry-run/--live", help="Preview without side effects (default), or --live to publish."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit the run report as JSON."),
) -> None:
    """Run the assembly line from a topic (or a source clip) to a fanned-out post."""
    if not topic and not source:
        ch.error("Nothing to run", "Give a --topic \"…\" or a --source <url|file>")
        raise typer.Exit(2)

    networks = [p.strip() for p in to.split(",") if p.strip()]
    if not networks:
        ch.error("No networks", "Pass --to x,telegram (at least one)")
        raise typer.Exit(2)
    _validate_kind(kind)

    from navig_pipeline.pipeline import run_pipeline

    dry = dry_run
    with ch.create_spinner("Running the assembly line…"):
        try:
            report = asyncio.run(run_pipeline(
                topic=topic, source=source, to=networks, kind=kind,
                narrate=narrate, campaign=campaign, url=url, dry_run=dry,
            ))
        except Exception as exc:  # noqa: BLE001
            ch.error("Pipeline failed", str(exc))
            raise typer.Exit(1) from exc

    if as_json:
        ch.raw_print(_json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        return

    # Stage ledger — every stage's outcome, nothing silent.
    table = ch.create_table(
        columns=[
            {"name": "stage", "style": "cyan"},
            {"name": "status"},
            {"name": "detail"},  # free-text column
        ],
    )
    for s in report.stages:
        table.add_row(s.name, _STATUS_GLYPH.get(s.status, s.status), s.detail)
    ch.print_table(table)

    if report.caption:
        ch.subheader("Drafted post")
        ch.raw_print(report.caption)
    if report.narration_path:
        ch.dim(f"🔊 voiceover: {report.narration_path}")

    if dry:
        ch.info("Dry run — nothing was published.", "Re-run with --live to publish.")
    else:
        total = len(report.receipts)
        ok = sum(1 for r in report.receipts if getattr(r, "ok", False))
        if total and ok == total:
            ch.success(f"Published to {ok}/{total} network(s)")
        elif ok:
            ch.warning(f"Published to {ok}/{total} network(s)", "Some networks failed — see the publish row above.")
        else:
            ch.error("Published to 0 networks", "All targets failed (or none configured) — see the publish row above.")


@pipeline_app.command("telegram-import")
def pipeline_telegram_import(
    channel: str = typer.Argument(..., help="Channel/chat id or @username to import from."),
    mode: str = typer.Option("auto", "--mode", help="auto | files | links"),
    from_sender: Optional[str] = typer.Option(None, "--from", help="Only this sender's media (the bot)."),
    limit: Optional[int] = typer.Option(None, "--limit", help="Max messages to scan/download."),
    preview: bool = typer.Option(False, "--preview", help="Scan only — counts, no side effects."),
    delete_after: bool = typer.Option(False, "--delete-after", help="Delete imported messages from the channel."),
    confirm: bool = typer.Option(False, "--confirm", help="Actually delete (needs --delete-after). Else dry-run."),
    skip_ocr: bool = typer.Option(False, "--skip-ocr", help="Skip slow video OCR."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Import a Telegram channel's media into the local library, then optionally delete the
    imported messages — verified-only (never deletes a message whose file isn't proven local)."""
    from navig_pipeline.telegram_import import preview_import, run_import

    if preview:
        rep = asyncio.run(preview_import(channel, mode=mode, from_sender=from_sender, limit=limit or 300))
        if as_json:
            ch.raw_print(_json.dumps(rep, indent=2, ensure_ascii=False))
            return
        if rep.get("error"):
            ch.error("Not ready", rep.get("setup_hint", ""))
            raise typer.Exit(2)
        ch.info(f"'{rep['title']}' — {rep['media_count']} media, {rep['link_count']} tiktok links "
                f"(mode: {rep['detected_mode']}) → would import {rep['would_import']}")
        return

    with ch.create_spinner("Importing from Telegram…"):
        rep = asyncio.run(run_import(channel, mode=mode, from_sender=from_sender, limit=limit,
                                     delete_after=delete_after, confirm_delete=confirm, skip_ocr=skip_ocr))
    if as_json:
        ch.raw_print(_json.dumps(rep, indent=2, ensure_ascii=False))
        return
    if rep.get("error"):
        ch.error("Not ready", rep.get("setup_hint", ""))
        raise typer.Exit(2)
    st = rep.get("status")
    if st == "empty":
        ch.warning("Nothing to import", rep.get("note", ""))
        return
    if st == "pipeline_failed":
        ch.error(f"Pipeline failed (exit {rep.get('pipeline_rc')})", (rep.get("pipeline_log") or "")[-400:])
        raise typer.Exit(1)
    kinds = ", ".join(f"{k}:{v}" for k, v in rep.get("by_kind", {}).items()) or "n/a"
    ch.success(f"Imported {rep['media']} media ({kinds}) → {rep['stage']}")
    d = rep.get("delete", {})
    if d.get("dry_run"):
        ch.info(f"DELETE DRY-RUN: {d['verified']}/{d['found']} messages verified in library. "
                f"Re-run with --delete-after --confirm to remove them.")
    elif d:
        ch.success(f"Cleaned channel: deleted {d['deleted']} verified message(s); "
                   f"{d.get('unverified_count', 0)} left unverified.")


@pipeline_app.command("schedule")
def pipeline_schedule(
    every: str = typer.Option(
        ..., "--every", "-e", help='Cadence — e.g. "every 7 days" or "0 9 * * 1" (Mon 09:00).'
    ),
    topic: Optional[str] = typer.Option(None, "--topic", "-t", help="What to write about."),
    source: Optional[str] = typer.Option(None, "--source", help="A media URL/file to transcribe first."),
    to: str = typer.Option("x,telegram", "--to", help="Comma-separated networks to publish to."),
    kind: str = typer.Option("social", "--kind", "-k", help="Post style."),
    narrate: bool = typer.Option(False, "--narrate", help="Also generate an audio voiceover."),
    campaign: Optional[str] = typer.Option(None, "--campaign", help="UTM campaign slug."),
    url: Optional[str] = typer.Option(None, "--url", help="Canonical hub URL to link."),
    name: Optional[str] = typer.Option(None, "--name", help="Cron job name (default: autopilot-<topic>)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the cron job without registering it."),
) -> None:
    """Autopilot — run + publish the assembly line on a recurring cadence.

    Registers a cron job that fires `navig pipeline run … --live` every cadence,
    so the pipeline re-drafts fresh content and fans it out hands-free. Manage the
    jobs with `navig cron list` / `navig cron remove <id>`.
    """
    if not topic and not source:
        ch.error("Nothing to schedule", "Give a --topic \"…\" or a --source <url|file>")
        raise typer.Exit(2)

    networks = [p.strip() for p in to.split(",") if p.strip()]
    if not networks:
        ch.error("No networks", "Pass --to x,telegram (at least one)")
        raise typer.Exit(2)
    _validate_kind(kind)

    from navig_pipeline.pipeline import autopilot_job_name, compose_run_command

    command = compose_run_command(
        topic=topic, source=source, to=networks, kind=kind,
        narrate=narrate, campaign=campaign, url=url,
    )
    job_name = autopilot_job_name(topic=topic, source=source, name=name)

    if dry_run:
        ch.info(
            f"Would schedule autopilot '{job_name}'",
            f"{every} → {command}",
        )
        ch.dim(f'Register it: navig cron add "{job_name}" "{every}" \'{command}\'')
        return

    # Register through the SAME gateway cron API `navig cron add` uses (no reinvention).
    # The PUBLIC seam, not core's private `navig.commands.cron._cron_base` — which is a
    # one-line wrapper over exactly this and was the only cross-package private import in
    # the plugin fleet. An underscore name is not a contract; it can be renamed without
    # notice, and the failure would land here as an ImportError at call time.
    # navig's import comes FIRST: without navig the daemon that runs the schedule does not
    # exist, and that — not a missing `requests`, which navig brings — is what to report.
    from navig.gateway_client import gateway_base_url

    import requests

    try:
        resp = requests.post(
            f"{gateway_base_url()}/cron/jobs",
            json={"name": job_name, "schedule": every, "command": command},
            timeout=5,
        )
    except requests.exceptions.RequestException:
        ch.error(
            "Gateway not reachable — can't schedule",
            "Start it (navig gateway start) and retry, or register manually:",
        )
        ch.dim(f'navig cron add "{job_name}" "{every}" \'{command}\'')
        raise typer.Exit(1) from None

    if resp.ok:
        ch.success(
            f"Autopilot scheduled: {job_name}",
            f"{every} → publishes to {', '.join(networks)}"
            + (" (with voiceover)" if narrate else ""),
        )
        ch.dim("Manage: navig cron list · navig cron remove <id> · navig pipeline schedule --dry-run to preview")
    else:
        ch.error("Could not schedule", (resp.text or "")[:200])
        raise typer.Exit(1)


@pipeline_app.command("reel")
def pipeline_reel(
    scenario: Path = typer.Argument(..., help="A scenario .md (the same format `navig audio` uses)."),
    lang: str = typer.Option(None, "--lang", "-l", help="Languages, e.g. fr,en. Default: the scenario's own."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Output directory."),
    music: Optional[Path] = typer.Option(None, "--music", help="Music bed laid under the voice."),
    duck: float = typer.Option(-12.0, "--duck", help="How far to drop the bed, in dB."),
    size: str = typer.Option("1080x1920", "--size", help="Frame size."),
    fps: int = typer.Option(30, "--fps"),
    port: Optional[int] = typer.Option(None, "--port", help="Reuse a running `navig cdp` browser."),
    captions: bool = typer.Option(True, "--captions/--no-captions", help="Burn the subtitles in."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Cost and shot plan only. Spends nothing."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """🎬 Build a vertical reel per language — capture, narrate, caption, score.

    Picture is cut to the voice: each track's shots are scaled to exactly how long its
    narration turned out, so a cut never lands mid-sentence.

        navig pipeline reel promo/grid.fr.md --lang fr,en --music theme.mp3
        navig pipeline reel promo/grid.fr.md --dry-run
    """
    from navig_pipeline.reel import ReelError, detect_capabilities, plan_shots, scenario_for

    if not scenario.exists():
        ch.error(f"scenario not found: {scenario}")
        raise typer.Exit(2)
    try:
        width, height = (int(p) for p in size.lower().split("x", 1))
    except ValueError:
        ch.error(f"--size must look like 1080x1920, got {size!r}")
        raise typer.Exit(2) from None
    if music and not music.exists():
        ch.error(f"music bed not found: {music}")
        raise typer.Exit(2)

    caps = detect_capabilities()
    missing = [name for name, ok in caps.items() if not ok]
    if missing and not dry_run:
        ch.error(
            f"cannot build a reel — missing: {', '.join(missing)}",
            "narrate needs navig-audio; capture needs a browser; assemble needs ffmpeg.",
        )
        raise typer.Exit(2)

    from navig_audio.podcast import cost as podcast_cost
    from navig_audio.podcast import scenario as podcast_scenario

    langs = [c.strip() for c in lang.split(",") if c.strip()] if lang else []
    out_dir = out or scenario.parent / "reels"

    try:
        primary = podcast_scenario.load(scenario)
    except Exception as exc:  # noqa: BLE001 - ScenarioError and friends carry the reason
        ch.error("Could not read the scenario", str(exc))
        raise typer.Exit(2) from exc
    if not langs:
        langs = [primary.lang]

    # ── dry run: the shot plan and the bill, before a credit is spent ──────────
    if dry_run:
        payload: dict = {"languages": langs, "tracks": [], "estimate": None}
        try:
            estimate = podcast_cost.estimate(primary, langs)
            payload["estimate"] = estimate.to_dict() if hasattr(estimate, "to_dict") else None
        except Exception as exc:  # noqa: BLE001 - an offline plan is still useful
            payload["estimate_error"] = str(exc)
        for track in primary.tracks:
            # Nothing has been spoken yet, so cut against the billing estimate of ~15
            # characters a second — enough to show the plan, never used for a real cut.
            guess = max(1.0, track.billable_chars / 15.0)
            try:
                planned, note = plan_shots(track.shots, guess, track=f"{track.number:02d}")
            except ReelError as exc:
                ch.error(f"track {track.number:02d}", str(exc))
                raise typer.Exit(2) from exc
            payload["tracks"].append({
                "number": track.number, "title": track.title,
                "estimated_s": round(guess, 1),
                "shots": [p.to_dict() for p in planned],
                "note": note,
            })
        if json_out:
            ch.raw_print(_json.dumps(payload, indent=2))
            return
        table = ch.create_table(columns=[
            {"name": "track", "style": "cyan"},
            {"name": "≈ spoken", "justify": "right"},
            {"name": "shots", "justify": "right"},
            {"name": "sources", "style": "dim"},
        ])
        for row in payload["tracks"]:
            sources = ", ".join(
                (s["url"] or s["image"] or "?").split("/")[-1][:28] for s in row["shots"]
            )
            table.add_row(f"{row['number']:02d} {row['title'][:26]}",
                          f"{row['estimated_s']}s", str(len(row["shots"])), sources)
        ch.print_table(table)
        est = payload.get("estimate") or {}
        if est:
            ch.dim(f"≈ {est.get('total_credits', '?')} credits for {', '.join(langs)}")
        ch.dim("Dry run — nothing captured, nothing spent.")
        return

    # ── the real build ────────────────────────────────────────────────────────
    from navig_pipeline.clip import needs_capture
    from navig_pipeline.reel import build_lang

    # A browser is started ONLY when a shot films a live URL (navig-browser, the `film` extra);
    # a reel of generated shots and stills needs no browser at all.
    owned_port: int | None = None
    active = port
    if active is None and needs_capture(primary):
        from navig_browser import cdp_actions

        launched = cdp_actions.new(app="chrome", headless=True,
                                   window_size=f"{width}x{height}")
        active = launched.get("port")
        owned_port = active
        if not active:
            ch.error("could not launch a browser to capture with", str(launched.get("error") or ""))
            raise typer.Exit(1)

    results = []
    try:
        for code in langs:
            path = scenario if code == primary.lang else scenario_for(scenario, code)
            if not path.exists():
                ch.error(f"no scenario for {code!r} at {path.name}",
                         f"Create it first:  {command_name('audio')} translate {scenario.name} --to {code}")
                raise typer.Exit(2)
            ch.dim(f"── {code} ──")
            results.append(asyncio.run(build_lang(
                path, out_dir, lang=code, port=active, width=width, height=height,
                fps=fps, music=music, duck_db=duck, captions=captions,
                progress=ch.dim,
            )))
    except ReelError as exc:
        ch.error("Reel build failed", str(exc))
        raise typer.Exit(1) from exc
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - provider / capture / ffmpeg all land here
        ch.error("Reel build failed", str(exc))
        raise typer.Exit(1) from exc
    finally:
        # Whatever happened, do not leave a browser we started running.
        if owned_port:
            try:
                from navig_browser import cdp_actions

                cdp_actions.stop(port=owned_port)
            except Exception:  # noqa: BLE001
                pass

    if json_out:
        ch.raw_print(_json.dumps([r.to_dict() for r in results], indent=2))
        return
    table = ch.create_table(columns=[
        {"name": "lang", "style": "cyan"},
        {"name": "file"},
        {"name": "length", "justify": "right"},
        {"name": "shots", "justify": "right"},
        {"name": "credits", "justify": "right"},
    ])
    for r in results:
        table.add_row(r.lang, r.path.name if r.path else "—",
                      f"{r.duration_s:.1f}s", str(r.shots), str(r.credits_spent))
    ch.print_table(table)
    for r in results:
        for note in r.notes:
            ch.dim(f"⚠ {note}")
    ch.success(f"{len(results)} reel(s) in {out_dir}")


@pipeline_app.command("clip")
def pipeline_clip(
    shotlist: Path = typer.Argument(..., help="A shotlist .md — a scenario with picture and no speech."),
    audio: Optional[Path] = typer.Option(None, "--audio", "-a", help="The track to cut to. Overrides the frontmatter's `audio:`."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Output directory."),
    look: Optional[str] = typer.Option(None, "--look", help="Format preset. Overrides the frontmatter's `look:`."),
    size: str = typer.Option("1080x1920", "--size", help="Frame size."),
    fps: int = typer.Option(30, "--fps"),
    captions: Optional[Path] = typer.Option(None, "--captions", help="An .srt/.ass to burn in (lyrics)."),
    model: Optional[str] = typer.Option(None, "--model", help="Replicate model for generated shots."),
    bpm: Optional[float] = typer.Option(None, "--bpm", help="Known tempo. A short cut has too few bars to detect one reliably."),
    audio_range: Optional[str] = typer.Option(None, "--range", help="Cut one passage of the track: START-END in seconds (e.g. 41.2-58.6). Overrides `audio_range:`."),
    port: Optional[int] = typer.Option(None, "--port", help="Reuse a running `navig cdp` browser."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Section plan and cost only. Spends nothing."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """🎵 Build a vertical clip cut to a track you already have — no narration.

    `reel` fits picture to a voice it renders. This fits picture to audio you supply, which
    is what a song needs. Pin the cuts to the music with `sections:` in the frontmatter,
    and bring your own grade with `look_spec:` if the built-in formats are not yours.

        navig pipeline clip sabdoza-01.md --dry-run
        navig pipeline clip sabdoza-01.md --audio mix.mp3 --look broadcast
    """
    from navig_pipeline.clip import (
        ClipError,
        audio_duration,
        detect_capabilities,
        generated_count,
        needs_capture,
        plan_sections,
        resolve_audio,
        resolve_overlays,
        resolve_range,
        resolve_video_settings,
        resolve_beats,
        window_grid,
    )
    from navig_pipeline.reel import DEFAULT_REPLICATE_MODEL

    if not shotlist.exists():
        ch.error(f"shotlist not found: {shotlist}")
        raise typer.Exit(2)
    try:
        width, height = (int(p) for p in size.lower().split("x", 1))
    except ValueError:
        ch.error(f"--size must look like 1080x1920, got {size!r}")
        raise typer.Exit(2) from None

    from navig_audio.podcast import scenario as podcast_scenario

    try:
        episode = podcast_scenario.load(shotlist)
    except Exception as exc:  # noqa: BLE001 - ScenarioError and friends carry the reason
        ch.error("Could not read the shotlist", str(exc))
        raise typer.Exit(2) from exc

    window_override: Optional[tuple[float, float]] = None
    if audio_range:
        try:
            lo, hi = (float(part) for part in audio_range.replace(" ", "").split("-", 1))
            window_override = (lo, hi)
        except ValueError:
            ch.error(f"--range must look like 41.2-58.6 (seconds), got {audio_range!r}")
            raise typer.Exit(2) from None

    try:
        track = resolve_audio(shotlist, episode, audio)
        # Tempo comes from the WHOLE track, then the window is taken from the result. A
        # short passage has too few bars to detect a tempo, and detecting on the excerpt
        # would make the dry run plan cuts the build would not reproduce.
        grid = resolve_beats(episode, track, bpm)
        window = resolve_range(episode, window_override)
        audio_s = audio_duration(track)
        if window is not None:
            start, end = window[0], min(window[1], audio_s)
            if start >= audio_s:
                ch.error(f"--range starts at {start:g}s but the track is {audio_s:.2f}s long")
                raise typer.Exit(2)
            audio_s = end - start
            grid = window_grid(grid, start, end)
        sections, notes = plan_sections(episode, audio_s, grid)
        # Resolved during the DRY RUN too, not only at build time. A mistyped overlay path
        # that passes the plan and fails the render is the one error the dry run exists to
        # catch, and it would surface only after the paid steps had already run.
        clip_overlays = resolve_overlays(shotlist, episode)
    except ClipError as exc:
        ch.error("Could not plan the clip", str(exc))
        raise typer.Exit(2) from exc

    caps = detect_capabilities(episode)
    missing = [name for name, ok in caps.items() if not ok]
    if missing and not dry_run:
        ch.error(
            f"cannot build a clip — missing: {', '.join(missing)}",
            "assemble needs ffmpeg on PATH; capture needs a browser (only for url= shots).",
        )
        raise typer.Exit(2)

    out_dir = out or shotlist.parent / "out"
    to_generate = generated_count(sections)
    # Resolve the model HERE, not just at build time: a dry run naming the wrong model is
    # the one line the user reads before deciding to spend, and a shotlist that picks its
    # own model would have been reported as the default.
    try:
        chosen_model, video_input, image_key = resolve_video_settings(episode, model)
    except ClipError as exc:
        ch.error("Could not read the video settings", str(exc))
        raise typer.Exit(2) from exc

    if dry_run:
        payload = {
            "shotlist": str(shotlist), "audio": str(track),
            "audio_range": list(window) if window else None,
            "overlays": [{"clip": str(o.path), "mode": o.mode, "opacity": o.opacity,
                          "start": o.start, "tint": o.tint,
                          "window": list(o.window) if o.window else None}
                         for o in clip_overlays],
            "audio_s": round(audio_s, 3), "look": look or episode.meta.get("look"),
            "pinned": bool(episode.meta.get("sections") is not None),
            "generate": to_generate, "model": chosen_model or DEFAULT_REPLICATE_MODEL,
            "video_input": video_input, "video_image_key": image_key,
            "sections": [s.to_dict() for s in sections], "notes": notes,
            "beat": grid.to_dict() if grid is not None else None,
        }
        if json_out:
            ch.raw_print(_json.dumps(payload, indent=2))
            return
        table = ch.create_table(columns=[
            {"name": "section", "style": "cyan"},
            {"name": "start", "justify": "right"},
            {"name": "on screen", "justify": "right"},
            {"name": "shots", "justify": "right"},
            {"name": "picture", "style": "dim"},
        ])
        for s in sections:
            kinds = ", ".join(f"{sh.source}:{sh.seconds:.1f}s" for sh in s.shots)
            table.add_row(f"{s.number:02d} {s.title[:24]}", f"{s.start:.2f}s",
                          f"{s.seconds:.2f}s", str(len(s.shots)), kinds[:44])
        ch.print_table(table)
        where = f" [{window[0]:g}s–{window[1]:g}s of the track]" if window else ""
        ch.dim(f"{track.name}{where} — {audio_s:.2f}s, "
               f"cuts {'pinned' if payload['pinned'] else 'weighted'}")
        if grid is not None:
            ch.dim(f"beat: {grid.bpm:.1f}bpm · {len(grid.beats)} beats · "
                   f"{len(grid.downbeats)} downbeats · confidence {grid.confidence:.2f}")
        for o in clip_overlays:
            ch.dim(f"overlay: {o.path.name} · {o.mode} @ {o.opacity:g}"
                   + (f" · tint {o.tint}" if o.tint else "")
                   + (f" · from {o.start:g}s" if o.start else "")
                   + (f" · only {o.window[0]:g}-{o.window[1]:g}s" if o.window else " · WHOLE CLIP"))
        for note in notes:
            ch.dim(f"⚠ {note}")
        if to_generate:
            seeded = sum(1 for s in sections for sh in s.shots if sh.prompt and sh.image)
            how = f"{to_generate} shot(s) would be generated on {payload['model']}"
            if seeded:
                how += f" ({seeded} seeded from a still — image-to-video)"
            ch.dim(f"{how} — the only paid step. Check credit: {command_name('vault', 'navig-vault')} test replicate")
        ch.dim("Dry run — nothing generated, nothing spent.")
        return

    from navig_pipeline.clip import build

    # A browser is started ONLY for a shotlist that actually films one. An all-generated
    # clip that launched Chrome would leave a window nobody asked for and nobody closes.
    # Filming drives a real browser through navig-browser (the `film` extra).
    owned_port: int | None = None
    active = port or 0
    if needs_capture(episode) and not port:
        from navig_browser import cdp_actions

        launched = cdp_actions.new(app="chrome", headless=True, window_size=f"{width}x{height}")
        active = launched.get("port")
        owned_port = active
        if not active:
            ch.error("could not launch a browser to capture with", str(launched.get("error") or ""))
            raise typer.Exit(1)

    try:
        result = asyncio.run(build(
            shotlist, track, out_dir, look_name=look, width=width, height=height,
            fps=fps, captions=captions, model=model, bpm=bpm,
            audio_range=window_override, port=active or 0,
            progress=ch.dim,
        ))
    except ClipError as exc:
        ch.error("Clip build failed", str(exc))
        raise typer.Exit(1) from exc
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - provider / capture / ffmpeg all land here
        ch.error("Clip build failed", str(exc))
        raise typer.Exit(1) from exc
    finally:
        if owned_port:
            try:
                from navig_browser import cdp_actions

                cdp_actions.stop(port=owned_port)
            except Exception:  # noqa: BLE001
                pass

    if json_out:
        ch.raw_print(_json.dumps(result.to_dict(), indent=2))
        return
    for note in result.notes:
        ch.dim(f"⚠ {note}")
    ch.success(
        f"{result.path.name if result.path else 'clip'} — {result.duration_s:.2f}s, "
        f"{width}x{height}, {result.shots} shot(s), {result.generated} generated",
        str(out_dir),
    )


@pipeline_app.command("captions")
def pipeline_captions(
    audio: Path = typer.Argument(..., help="The track to transcribe."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Where to write the .srt (default: next to the audio)."),
    language: Optional[str] = typer.Option(None, "--language", "-l", help="ISO code, e.g. ru/fr/en. Omit to let the model detect it — forcing the wrong one returns a placeholder, not a transcript."),
    offset: float = typer.Option(0.0, "--offset", help="Shift every timestamp by this many seconds — use the passage start when captioning an excerpt."),
    no_chunk: bool = typer.Option(False, "--no-chunk", help="One request for the whole file. Cheaper, and it silently loses dense passages on music — see the note in captions.py."),
    json_out: bool = typer.Option(False, "--json", help="Word-level timings as JSON instead of an .srt."),
) -> None:
    """✍️  Transcribe a track to an .srt — with word-level timings.

    `navig voice transcribe` gives you the words. This gives you the words *and when they
    land*, which is what a caption burn and a passage cut both need.

        navig pipeline captions track.mp3
        navig pipeline captions track.mp3 --json > words.json
    """
    from navig_pipeline.captions import CaptionError, to_srt, transcribe

    if not audio.exists():
        ch.error(f"audio not found: {audio}")
        raise typer.Exit(2)

    try:
        # No progress lines under --json: chunk notices on stdout make the output
        # unparseable, which is the whole point of the flag.
        transcript = transcribe(audio, language=language,
                                chunked=False if no_chunk else None,
                                progress=None if json_out else ch.dim)
    except CaptionError as exc:
        ch.error("Could not transcribe", str(exc))
        raise typer.Exit(1) from exc
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - httpx / provider errors land here
        ch.error("Could not transcribe", str(exc))
        raise typer.Exit(1) from exc

    if json_out:
        ch.raw_print(_json.dumps({
            "language": transcript.language,
            "duration_s": round(transcript.duration, 3),
            "text": transcript.text,
            "dropped": transcript.dropped,
            "words": [{"text": w.text, "start": round(w.start, 3), "end": round(w.end, 3)}
                      for w in transcript.words],
            "lines": [{"text": ln.text, "start": round(ln.start, 3), "end": round(ln.end, 3)}
                      for ln in transcript.lines],
        }, ensure_ascii=False, indent=2))
        return

    dst = out or audio.with_suffix(".srt")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(to_srt(transcript.lines, offset=offset), encoding="utf-8")
    for line in transcript.dropped:
        ch.dim(f"dropped a hallucinated credit line: {line!r}")
    ch.success(
        f"{dst.name} — {len(transcript.lines)} line(s), {transcript.word_count} word(s), "
        f"detected {transcript.language}",
        str(dst),
    )


@pipeline_app.command("hooks")
def pipeline_hooks(
    audio: Path = typer.Argument(..., help="The track to search for cuttable passages."),
    top: int = typer.Option(6, "--top", "-n", help="How many candidates to show."),
    min_s: float = typer.Option(10.0, "--min", help="Shortest passage, in seconds."),
    max_s: float = typer.Option(25.0, "--max", help="Longest passage, in seconds."),
    language: Optional[str] = typer.Option(None, "--language", "-l", help="ISO code. Omit to detect."),
    bpm: Optional[float] = typer.Option(None, "--bpm", help="Known tempo, so boundaries snap to a real grid."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """🎣 Rank the passages of a track worth cutting a short clip from.

    A shortlist for a person to choose from — NOT a prediction. It measures four things a
    hook actually has (repeated wording, word density, onset energy, clean phrase edges),
    reports each separately, and prints the words so the choice is made on lyrics.

    Feed a winner straight back in:

        navig pipeline hooks track.mp3
        navig pipeline clip shot.md --range 41.2-58.6
    """
    from navig_pipeline.captions import CaptionError, transcribe
    from navig_pipeline.hooks import rank

    if not audio.exists():
        ch.error(f"audio not found: {audio}")
        raise typer.Exit(2)
    if max_s <= min_s:
        ch.error(f"--max ({max_s:g}s) must be greater than --min ({min_s:g}s)")
        raise typer.Exit(2)

    try:
        transcript = transcribe(audio, language=language,
                                progress=None if json_out else ch.dim)
    except CaptionError as exc:
        ch.error("Could not transcribe", str(exc))
        raise typer.Exit(1) from exc
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001
        ch.error("Could not transcribe", str(exc))
        raise typer.Exit(1) from exc

    grid = None
    try:
        from navig_generate.media.beats import detect

        grid = detect(audio, bpm=bpm)
    except Exception as exc:  # noqa: BLE001 - the grid only refines boundaries
        ch.dim(f"no beat grid ({exc}) — boundaries stay where the words put them")

    passages = rank(transcript, audio, top=top, min_s=min_s, max_s=max_s, grid=grid)
    if not passages:
        ch.warning(
            f"no passage between {min_s:g}s and {max_s:g}s starts and ends on a phrase",
            "Widen the window with --min/--max, or the track may have no pauses to cut on.",
        )
        return

    if json_out:
        ch.raw_print(_json.dumps([{
            "start": round(p.start, 2), "end": round(p.end, 2),
            "duration_s": round(p.duration, 2), "score": round(p.score, 3),
            "parts": {k: round(v, 3) for k, v in p.parts.items()}, "text": p.text,
        } for p in passages], ensure_ascii=False, indent=2))
        return

    table = ch.create_table(columns=[
        {"name": "#", "justify": "right"},
        {"name": "passage", "style": "cyan"},
        {"name": "len", "justify": "right"},
        {"name": "score", "justify": "right"},
        {"name": "repeat", "justify": "right", "style": "dim"},
        {"name": "words/s", "justify": "right", "style": "dim"},
        {"name": "energy", "justify": "right", "style": "dim"},
    ])
    for n, p in enumerate(passages, start=1):
        table.add_row(
            str(n), f"{p.start:.1f}s–{p.end:.1f}s", f"{p.duration:.1f}s",
            f"{p.score:.2f}", f"{p.parts.get('repeat', 0):.2f}",
            f"{p.parts.get('density', 0):.2f}", f"{p.parts.get('energy', 0):.2f}",
        )
    ch.print_table(table)
    for n, p in enumerate(passages, start=1):
        ch.dim(f"{n}. {p.text[:150]}")
    ch.dim(
        f"detected {transcript.language} · {transcript.word_count} words"
        + (f" · {grid.bpm:.1f}bpm" if grid is not None else "")
    )
    ch.dim("A shortlist, not a prediction — read the words and pick. "
           f"Then: {CMD} clip <shotlist>.md --range <start>-<end>")
