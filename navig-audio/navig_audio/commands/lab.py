"""Audio Lab commands — delivery, clean-video, match, loop, podcast-edit, stems, similar, dub, score.

  navig audio delivery mix.wav --target spotify                 meets the spec?
  navig audio delivery mix.wav --target ebu-r128 --fix -f wav   level it to fit
  navig audio clean-video interview.mp4                         clean the voice, keep it in sync
  navig audio match mix.wav --ref reference.mp3                 tone + loudness like the reference
  navig audio loop rain --seconds 600                           a local, seamless 10-minute bed
  navig audio loop "night café, distant chatter" --seconds 300 --generate --allow-cloud
  navig audio podcast-edit episode.mp3                         propose cuts (review the JSON)
  navig audio podcast-edit episode.cuts.json --apply           apply the reviewed cuts
  navig audio stems song.mp3 --allow-cloud                     split once (ElevenLabs)
  navig audio stems song.stems --preset karaoke                remix locally
  navig audio similar door.wav --in D:/sfx                     sounds like this one
  navig audio dub talk.mp4 --to fr --to es --allow-cloud       same voices, other languages
  navig audio score trailer.mp4 --style "dark synth" --dry-run music that follows the cuts

Mounted on ``audio_app`` from ``audio.py``. Restoration of old media is
``navig audio enhance --preset audio-enhance/{cassette,vinyl,phone-call,voice-memo}``.
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Optional

import typer

from navig_audio._typed_path import resolve_user_path
from navig_audio.commands.audio import audio_app
from navig_sdk import console as ch


def _src(p: Path) -> Path:
    src = resolve_user_path(p)
    if not src.is_file():
        ch.error(f"No such file: {src}", "A relative path resolves against the directory you typed it in.")
        raise typer.Exit(2)
    return src


@audio_app.command("delivery")
def audio_delivery(
    file: Path = typer.Argument(..., help="The file to check."),
    target: str = typer.Option("youtube", "--target", "-t",
                               help="youtube · spotify · apple-music · tidal · podcast · podcast-mono · ebu-r128 · atsc-a85 · all"),
    fix: bool = typer.Option(False, "--fix", help="Write a levelled copy that meets the target."),
    fmt: str = typer.Option("wav", "--format", "-f", help="wav · wav16 · flac · mp3 (for --fix)"),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Output folder for --fix."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """📡 Check loudness against a platform's delivery spec — and fix it to fit."""
    from navig_audio.lab.delivery import TARGETS, DeliveryError, check
    from navig_audio.lab.delivery import fix as do_fix

    src = _src(file)
    targets = list(TARGETS) if target == "all" else [target]
    try:
        checks = [check(src, t) for t in targets]
    except DeliveryError as exc:
        ch.error("Delivery check failed", str(exc))
        raise typer.Exit(2) from None
    fixed = None
    if fix:
        if target == "all":
            ch.error("--fix needs one --target")
            raise typer.Exit(2)
        try:
            fixed = do_fix(src, target, out_dir=resolve_user_path(out) if out else None, fmt=fmt)
        except DeliveryError as exc:
            ch.error("Could not fix", str(exc))
            raise typer.Exit(1) from None
    if as_json:
        ch.raw_print(_json.dumps({"checks": [c.to_dict() for c in checks],
                                  "fixed": {"file": str(fixed[0]), "check": fixed[1].to_dict()} if fixed else None},
                                 indent=2, ensure_ascii=False))
        raise typer.Exit(0 if all(c.ok for c in checks) or (fixed and fixed[1].ok) else 1)
    table = ch.create_table(columns=[
        {"name": "target", "no_wrap": True}, {"name": "", "no_wrap": True},
        {"name": "LUFS", "no_wrap": True, "justify": "right"}, {"name": "true peak", "no_wrap": True, "justify": "right"},
        {"name": "what it means"},
    ])
    for c in checks:
        mark = "[green]✓[/green]" if c.ok else "[red]✗[/red]"
        table.add_row(c.target, mark, f"{c.lufs:.1f} / {c.target_lufs:.0f}", f"{c.true_peak_db:+.1f} / {c.ceiling_db:+.1f}", c.advice)
    ch.print_table(table)
    if fixed:
        ch.success(f"Saved {fixed[0]} — {fixed[1].lufs:.1f} LUFS, {fixed[1].true_peak_db:+.1f} dBTP ({fixed[1].verdict})")
    elif not all(c.ok for c in checks) and target != "all":
        ch.info(f"Next: navig audio delivery \"{file}\" --target {target} --fix")


@audio_app.command("clean-video")
def audio_clean_video(
    video: Path = typer.Argument(..., help="The video whose dialogue to clean (left untouched)."),
    preset: str = typer.Option("audio-enhance/voice-restoration", "--preset", "-p", help="An audio.enhance preset."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Output folder (default: next to the video)."),
    pcm: bool = typer.Option(False, "--pcm", help="Keep the cleaned sound uncompressed (MOV/MKV only)."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """🎬 Clean the dialogue in a video and put it back in sync (picture copied untouched)."""
    from navig_audio.lab.video import VideoCleanError, clean_video

    src = _src(video)
    try:
        res = clean_video(src, preset_id=preset, out_dir=resolve_user_path(out) if out else None, pcm=pcm,
                          progress=None if as_json else (lambda m, _f: ch.dim(f"… {m}")))
    except VideoCleanError as exc:
        ch.error("Video cleanup failed", str(exc))
        raise typer.Exit(1) from None
    if as_json:
        ch.raw_print(_json.dumps(res.to_dict(), indent=2, ensure_ascii=False))
        raise typer.Exit(0 if res.verdict in ("pass", "warn", "clean") else 1)
    if res.verdict == "clean":
        ch.info("The dialogue has no measurable defects — the video is left as it is.")
        return
    if res.verdict == "fail" or not res.output:
        ch.error("Cleanup rejected — it would have changed the dialogue", res.enhance.get("summary", ""))
        raise typer.Exit(1)
    ch.success(f"Saved {res.output}")
    ch.dim(f"steps: {', '.join(res.enhance['stages'])} · sync drift {res.sync.get('drift_s', 0) * 1000:.0f} ms")


@audio_app.command("match")
def audio_match(
    file: Path = typer.Argument(..., help="Your track (left untouched)."),
    ref: Path = typer.Option(..., "--ref", "-r", help="The reference track to sound like."),
    strength: float = typer.Option(0.7, "--strength", "-s", min=0.1, max=1.0, help="How far toward the reference's tone."),
    match_dynamics: bool = typer.Option(False, "--match-dynamics", help="Also compress toward the reference's punch."),
    fmt: str = typer.Option("wav", "--format", "-f"),
    out: Optional[Path] = typer.Option(None, "--out", "-o"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """🎚 Match a reference's tonal balance and loudness (dynamics kept unless asked)."""
    from navig_audio.lab.match import MatchError, match_reference

    try:
        res = match_reference(_src(file), _src(ref), out_dir=resolve_user_path(out) if out else None,
                              strength=strength, match_dynamics=match_dynamics, fmt=fmt)
    except MatchError as exc:
        ch.error("Match failed", str(exc))
        raise typer.Exit(1) from None
    if as_json:
        ch.raw_print(_json.dumps(res.to_dict(), indent=2, ensure_ascii=False))
        return
    ch.success(f"Saved {res.output}")
    ch.dim(f"tone distance to the reference: {res.tone_distance_before_db:.1f} dB → {res.tone_distance_after_db:.1f} dB · "
           f"loudness {res.lufs_before:.1f} → {res.lufs_after:.1f} LUFS (reference {res.lufs_reference:.1f}) · dynamics {res.dynamics}")
    if res.eq_db:
        ch.dim("EQ: " + ", ".join(f"{k} {v:+.1f}" for k, v in res.eq_db.items()))


@audio_app.command("loop")
def audio_loop(
    what: str = typer.Argument(..., help="Local: rain · wind · server-room · room-tone · pink · brown · white. "
                                          "With --generate: any description."),
    seconds: float = typer.Option(300, "--seconds", "-s", help="Length of the render."),
    generate: bool = typer.Option(False, "--generate", help="Generate with ElevenLabs instead of local noise (billed)."),
    allow_cloud: bool = typer.Option(False, "--allow-cloud", help="Allow the billing --generate needs."),
    seed: int = typer.Option(7, "--seed", help="Local beds: a different seed, a different bed."),
    out: Optional[Path] = typer.Option(None, "--out", "-o"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """🌧 A seamless, loopable ambience bed at any length (rain, room tone, server room, …)."""
    from navig_audio.lab.ambience import AmbienceError, ambience

    dest = resolve_user_path(out) if out else Path.cwd()
    try:
        res = ambience(what, seconds=seconds, out_dir=dest, source="generate" if generate else "synth",
                       allow_cloud=allow_cloud, seed=seed)
    except AmbienceError as exc:
        ch.error("Ambience failed", str(exc))
        raise typer.Exit(2 if "allow cloud" in str(exc) or "local beds" in str(exc) else 1) from None
    if as_json:
        ch.raw_print(_json.dumps(res.to_dict(), indent=2, ensure_ascii=False))
        return
    ch.success(f"Loop: {res.loop} ({res.loop_seconds:.1f} s)")
    ch.success(f"Render: {res.render} ({res.seconds:.0f} s)")
    ch.dim(f"seam: {'smooth' if res.seam['smooth'] else 'audible — try another seed'} "
           f"(level step {res.seam['level_step_db']} dB) · {res.prompt}")


@audio_app.command("podcast-edit")
def audio_podcast_edit(
    file: Path = typer.Argument(..., help="The episode (left untouched) — or a reviewed .cuts.json with --apply."),
    apply: bool = typer.Option(False, "--apply", help="Apply a cut list (the reviewed JSON) instead of proposing one."),
    max_pause: float = typer.Option(1.2, "--max-pause", help="Pauses longer than this (s) are shortened."),
    keep_pause: float = typer.Option(0.5, "--keep-pause", help="How much of a long pause is kept (s)."),
    no_fillers: bool = typer.Option(False, "--no-fillers", help="Leave um / uh in."),
    no_repeats: bool = typer.Option(False, "--no-repeats", help="Leave repeated words in."),
    language: Optional[str] = typer.Option(None, "--language", "-l", help="Spoken language (default: detect)."),
    model: str = typer.Option("small", "--model", help="Local Whisper model: tiny · base · small · medium · large-v3"),
    out: Optional[Path] = typer.Option(None, "--out", "-o"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """✂️ Find fillers, long pauses and repeats — review the cut list, then apply it."""
    from navig_audio.lab.podcast_edit import CutList, PodcastEditError, analyse, apply_cuts

    src = _src(file)
    dest = resolve_user_path(out) if out else None
    try:
        if apply:
            cl = CutList.from_dict(_json.loads(src.read_text(encoding="utf-8")))
            dst, removed = apply_cuts(cl, out_dir=dest)
            if as_json:
                ch.raw_print(_json.dumps({"output": str(dst), "removed_s": round(removed, 2)}))
                return
            ch.success(f"Saved {dst} — {removed:.1f} s shorter")
            return
        cl = analyse(src, model=model, language=language, max_pause=max_pause, keep_pause=keep_pause,
                     cut_fillers=not no_fillers, cut_repeats=not no_repeats)
    except (PodcastEditError, OSError, ValueError) as exc:
        ch.error("Podcast edit failed", str(exc))
        raise typer.Exit(1) from None
    folder = dest or src.parent
    folder.mkdir(parents=True, exist_ok=True)
    from navig_audio.mastering.master import unique_path

    cut_file = unique_path(folder / f"{src.stem}.cuts.json")
    cut_file.write_text(_json.dumps(cl.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    if as_json:
        ch.raw_print(_json.dumps({**cl.to_dict(), "cut_list": str(cut_file)}, indent=2, ensure_ascii=False))
        return
    if not cl.cuts:
        ch.info(f"Nothing to cut in {cl.duration_s:.0f} s ({cl.language or 'unknown language'}). {cl.note}")
        return
    table = ch.create_table(columns=[{"name": "at", "no_wrap": True, "justify": "right"}, {"name": "kind", "no_wrap": True},
                                     {"name": "cut", "no_wrap": True, "justify": "right"}, {"name": "what"}])
    for c in cl.cuts:
        table.add_row(f"{int(c.start // 60)}:{c.start % 60:05.2f}", c.kind, f"{c.seconds:.2f}s", c.text)
    ch.print_table(table)
    total = cl.to_dict()["removable_s"]
    ch.info(f"{len(cl.cuts)} cuts · {total:.1f} s of {cl.duration_s:.0f} s · {cl.note}")
    ch.info(f"Next: review {cut_file} (set \"keep\": true to keep a cut), then: "
            f"navig audio podcast-edit \"{cut_file}\" --apply")


@audio_app.command("stems")
def audio_stems(
    target: Path = typer.Argument(..., help="A song to split — or a .stems folder to remix."),
    two: bool = typer.Option(False, "--two", help="Split into vocals + instrumental only (default: six parts)."),
    allow_cloud: bool = typer.Option(False, "--allow-cloud", help="Splitting uploads the song to ElevenLabs (billed)."),
    preset: Optional[str] = typer.Option(None, "--preset", "-p", help="karaoke · instrumental · acapella · drumless · bassless"),
    mute: list[str] = typer.Option([], "--mute", help="Mute a part (repeatable): vocals, drums, bass…"),
    solo: list[str] = typer.Option([], "--solo", help="Play only these parts (repeatable)."),
    gain: list[str] = typer.Option([], "--gain", help="Re-balance a part, e.g. --gain vocals=-3 (dB, repeatable)."),
    fmt: str = typer.Option("wav", "--format", "-f"),
    out: Optional[Path] = typer.Option(None, "--out", "-o"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """🎛 Split a song into parts once, then mute / solo / re-balance them locally."""
    from navig_audio.lab.stems import StemsError, mix, split

    path = resolve_user_path(target)
    try:
        if path.is_dir():
            gains = {}
            for g in gain:
                name, _, val = g.partition("=")
                gains[name] = float(val)
            dst = mix(path, preset=preset, mute=mute, solo=solo, gains=gains,
                      out=resolve_user_path(out) if out else None, fmt=fmt)
            if as_json:
                ch.raw_print(_json.dumps({"output": str(dst)}))
                return
            ch.success(f"Saved {dst}")
            return
        res = split(_src(path), six=not two, allow_cloud=allow_cloud, out_dir=resolve_user_path(out) if out else None)
    except StemsError as exc:
        ch.error("Stems failed", str(exc))
        raise typer.Exit(2 if "allow cloud" in str(exc) else 1) from None
    except ValueError:
        ch.error("--gain wants name=dB, e.g. --gain vocals=-3")
        raise typer.Exit(2) from None
    if as_json:
        ch.raw_print(_json.dumps(res.to_dict(), indent=2, ensure_ascii=False))
        return
    ch.success(f"Split into {len(res.stems)} parts: {', '.join(res.stems)}")
    ch.info(f"Next: navig audio stems \"{res.folder}\" --preset karaoke")


@audio_app.command("similar")
def audio_similar(
    query: Path = typer.Argument(..., help="The sound to find look-alikes for."),
    folder: Path = typer.Option(..., "--in", help="The folder (library) to search."),
    top: int = typer.Option(10, "--top", "-n", min=1, max=200),
    reindex: bool = typer.Option(False, "--reindex", help="Re-fingerprint every file."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """🔎 Find sounds that sound like this one — local fingerprints, cached."""
    from navig_audio.lab.similar import SimilarError, search

    try:
        matches, computed = search(_src(query), resolve_user_path(folder), top=top, reindex=reindex)
    except SimilarError as exc:
        ch.error("Search failed", str(exc))
        raise typer.Exit(1) from None
    if as_json:
        ch.raw_print(_json.dumps({"matches": [m.to_dict() for m in matches], "fingerprinted": computed}, indent=2))
        return
    table = ch.create_table(columns=[{"name": "#", "no_wrap": True, "justify": "right"},
                                     {"name": "match", "no_wrap": True, "justify": "right"}, {"name": "file"}])
    root = resolve_user_path(folder)
    for i, m in enumerate(matches, 1):
        try:
            shown = str(Path(m.path).relative_to(root))
        except ValueError:
            shown = m.path
        table.add_row(str(i), f"{max(0.0, m.score) * 100:.0f}%", shown)
    ch.print_table(table)
    ch.dim(f"{computed} new fingerprint(s) · the index is cached; --reindex rebuilds it")


@audio_app.command("dub")
def audio_dub(
    file: Path = typer.Argument(..., help="An audio file or video to dub (left untouched)."),
    to: list[str] = typer.Option(..., "--to", help="Target language code (repeatable): fr, es, de, ja, …"),
    source_lang: Optional[str] = typer.Option(None, "--from", help="Spoken language (default: detect)."),
    speakers: int = typer.Option(0, "--speakers", min=0, max=32, help="How many speakers (0 = detect)."),
    replace_track: bool = typer.Option(False, "--replace-track", help="Video: drop the original sound track."),
    allow_cloud: bool = typer.Option(False, "--allow-cloud", help="Dubbing uploads the sound to ElevenLabs (billed)."),
    out: Optional[Path] = typer.Option(None, "--out", "-o"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """🌍 Dub into other languages — the same voices, the timing kept."""
    from navig_audio.lab.dub import DubError, dub

    try:
        res = dub(_src(file), to, allow_cloud=allow_cloud, source_lang=source_lang, num_speakers=speakers,
                  out_dir=resolve_user_path(out) if out else None, keep_original_track=not replace_track,
                  progress=None if as_json else (lambda m: ch.dim(f"… {m}")))
    except DubError as exc:
        ch.error("Dubbing failed", str(exc))
        raise typer.Exit(2 if "allow cloud" in str(exc) else 1) from None
    if as_json:
        ch.raw_print(_json.dumps(res.to_dict(), indent=2, ensure_ascii=False))
        raise typer.Exit(0 if res.takes and not res.failed else 1)
    for t in res.takes:
        sync = "[green]in sync[/green]" if t.in_sync else f"[yellow]length off by {t.drift * 100:+.1f}%[/yellow]"
        ch.success(f"{t.language}: {t.output}")
        ch.console.print(f"   {t.duration_s:.1f} s · {sync}")
    for lang, why in res.failed.items():
        ch.error(f"{lang}: not dubbed", why)
    if res.failed:
        raise typer.Exit(1)


@audio_app.command("score")
def audio_score(
    video: Path = typer.Argument(..., help="The video to score (left untouched)."),
    style: str = typer.Option("cinematic underscore, warm strings, subtle pulse", "--style", "-s",
                              help="The music, in words (comma-separated styles)."),
    threshold: float = typer.Option(0.3, "--threshold", min=0.05, max=0.9, help="Scene-cut sensitivity (lower = more cuts)."),
    no_duck: bool = typer.Option(False, "--no-duck", help="Replace the sound of the video instead of ducking under it."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the cuts and the music plan; compose nothing."),
    allow_cloud: bool = typer.Option(False, "--allow-cloud", help="Composing is billed by ElevenLabs."),
    out: Optional[Path] = typer.Option(None, "--out", "-o"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """🎼 Compose music that follows the cuts of a video, mixed under its dialogue."""
    from navig_audio.lab.score import ScoreError, score_to_picture

    try:
        res = score_to_picture(_src(video), style, allow_cloud=allow_cloud, out_dir=resolve_user_path(out) if out else None,
                               threshold=threshold, duck=not no_duck, dry_run=dry_run,
                               progress=None if as_json else (lambda m: ch.dim(f"… {m}")))
    except ScoreError as exc:
        ch.error("Score failed", str(exc))
        raise typer.Exit(2 if "allow cloud" in str(exc) else 1) from None
    if as_json:
        ch.raw_print(_json.dumps(res.to_dict(), indent=2, ensure_ascii=False))
        return
    table = ch.create_table(columns=[{"name": "#", "no_wrap": True, "justify": "right"},
                                     {"name": "from", "no_wrap": True, "justify": "right"},
                                     {"name": "to", "no_wrap": True, "justify": "right"}, {"name": "music"}])
    for i, ((a, b), sec) in enumerate(zip(res.scenes, res.plan["sections"]), 1):
        table.add_row(str(i), f"{a:.1f}s", f"{b:.1f}s", ", ".join(sec["positive_local_styles"]))
    ch.print_table(table)
    if dry_run:
        ch.info(f"{len(res.cuts)} cut(s) → {len(res.scenes)} section(s). Next: the same command with --allow-cloud")
        return
    ch.success(f"Saved {res.output}")
    ch.dim(f"music alone: {res.music} · {'ducked under the dialogue' if res.ducked else 'music only'}")
