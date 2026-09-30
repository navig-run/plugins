"""``navig audio beat`` — beats to rap on: analyse a reference, preview a plan, render.

  navig audio beat analyse ref.mp3 [--bpm 76] [--card ref.md]   what is this beat (tempo · key · structure · spectrum)
  navig audio beat styles [--styles house.yaml]                 the presets available
  navig audio beat plan --style boom-bap-dark --bpm 76          the arrangement, section by section — costs nothing
  navig audio beat gen  --style boom-bap-dark --bpm 76 -n 3     render, measure, catalogue

``gen`` is the only verb that spends credits, and it says how many seconds it is about to
ask for before it asks. ``--dry-run`` shows the exact request and stops. Every render
leaves a ``.json`` beside the mp3 and a row in the folder's ``INDEX.md`` with the tempo
and key *measured* from the file, so a beat that came back at the wrong tempo is flagged
rather than discovered on the first take.

Mounted on ``audio_app`` by import side-effect from ``audio.py``, like ``episode``.
"""

from __future__ import annotations

import asyncio
import json as _json
from pathlib import Path
from typing import Optional

import typer

from navig_sdk import console as ch
from navig_audio.commands.audio import CMD, audio_app, key_hint
from navig_audio._typed_path import resolve_user_path


beat_app = typer.Typer(
    name="beat",
    help="🥁 Beats to rap on — analyse a reference, preview the plan for free, render with a known tempo and key.",
    no_args_is_help=True,
)
audio_app.add_typer(beat_app, name="beat")

_MODELS = ("music_v1", "music_v2", "music_v2_5")


def _styles_or_exit(files: list[Path] | None):
    from navig_audio.beats.styles import StyleError, load_styles

    try:
        return load_styles([Path(f) for f in files] if files else None)
    except StyleError as exc:
        ch.error("Cannot load beat styles", str(exc))
        raise typer.Exit(2) from exc


def _style_or_exit(styles, sid: str):
    from navig_audio.beats.styles import StyleError, get_style

    try:
        return get_style(styles, sid)
    except StyleError as exc:
        ch.error("Unknown style", str(exc))
        raise typer.Exit(2) from exc


def _split_csv(value: Optional[str]) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


@beat_app.command("analyse")
@beat_app.command("analyze", hidden=True)
def beat_analyse(
    file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                                help="The reference track (mp3, wav, m4a, …)."),
    bpm: Optional[float] = typer.Option(None, "--bpm", help="Tempo hint when you already know it (short cuts come back at a multiple)."),
    card: Optional[Path] = typer.Option(None, "--card", help="Write the beat card (markdown) here."),
    title: Optional[str] = typer.Option(None, "--title", help="Title for the card (default: the file name)."),
    bars: bool = typer.Option(False, "--bars", help="Add the bar-by-bar map: where the full beat enters, drops out, ends — with timecodes."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable profile."),
):
    """🔍 What is this beat — tempo, key, structure, spectrum, and a style brief derived from them.

    With ``--bars`` it also measures the arrangement bar by bar — the cue sheet lyrics are
    timed from. Pass the tempo a rapper counts (``--bpm 70`` for a 140 halftime beat).
    """
    from navig_generate.media.beats import BeatError
    from navig_generate.media.tonality import profile

    bm = None
    try:
        with ch.create_spinner("Listening…"):
            p = profile(file, bpm=bpm)
            if bars:
                from navig_audio.beats.bars import bar_map

                bm = bar_map(file, float(bpm or p.bpm_felt))
    except BeatError as exc:
        ch.error("Could not analyse the track", str(exc))
        raise typer.Exit(1) from exc

    if card is not None:
        card.parent.mkdir(parents=True, exist_ok=True)
        text = p.to_markdown(title)
        if bm is not None:
            text += "\n" + bm.to_markdown()
        card.write_text(text, encoding="utf-8")

    if as_json:
        d = p.to_dict()
        if bm is not None:
            d["bars"] = bm.to_dict()
        ch.raw_print(_json.dumps(d, indent=2, ensure_ascii=False))
        return

    table = ch.create_table(columns=[{"name": "measure", "style": "cyan"}, {"name": "value"}])
    tempo = f"{p.bpm_felt:.1f} bpm"
    if p.bpm_felt != p.bpm:
        tempo += f"  (detected {p.bpm:.1f} — double-time hi-hats)"
    table.add_row("tempo", f"{tempo}  · confidence {p.beat_confidence:.2f}")
    key = f"{p.key.name}  · confidence {p.key.confidence:.2f}"
    if p.key.bass_root:
        key += f"  · bass on {p.key.bass_root}"
    table.add_row("key", key)
    table.add_row("length", f"{p.duration_s:.1f} s")
    table.add_row("loudness", f"RMS {p.loudness_dbfs:.1f} dBFS · peak {p.peak_dbfs:.1f} dBFS")
    table.add_row("spectrum", " · ".join(f"{k} {v * 100:.0f}%" for k, v in p.bands.items()))
    table.add_row("structure", " → ".join(f"{s.label} {s.start:.0f}–{s.end:.0f}s" for s in p.sections))
    ch.print_table(table)
    ch.dim(f"brief: {p.style_brief()}")
    if bm is not None:
        ch.raw_print("\n".join(bm.strip()))
        cues = ch.create_table(columns=[
            {"name": "block", "style": "cyan"}, {"name": "bars", "justify": "right"},
            {"name": "from", "justify": "right"}, {"name": "to", "justify": "right"},
        ])
        for b in bm.blocks:
            cues.add_row(b.label, f"{b.start_bar}–{b.end_bar} ({b.bars})", f"{b.start_s:.1f}s", f"{b.end_s:.1f}s")
        ch.print_table(cues)
    if card is not None:
        ch.success(f"Beat card → {card}")


@beat_app.command("styles")
def beat_styles(
    styles: Optional[list[Path]] = typer.Option(None, "--styles", "-s", help="Extra styles YAML (repeatable; later files win)."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable list."),
):
    """📚 The beat styles available — built-in genres plus any file you pass."""
    catalog = _styles_or_exit(styles)
    if as_json:
        ch.raw_print(_json.dumps({k: v.to_dict() for k, v in catalog.items()}, indent=2, ensure_ascii=False))
        return
    table = ch.create_table(columns=[
        {"name": "id", "style": "cyan"},
        {"name": "label"},
        {"name": "bpm", "justify": "right"},
        {"name": "key"},
        {"name": "lang", "style": "dim"},
        {"name": "source", "style": "dim"},
    ])
    for sid, s in sorted(catalog.items()):
        table.add_row(sid, s.label, f"{s.bpm:g}", s.key, s.language or "—", s.source)
    ch.print_table(table)
    ch.dim(f"{CMD} beat plan --style <id>   (free)   ·   {CMD} beat gen --style <id> -n 3")


@beat_app.command("plan")
def beat_plan(
    style: str = typer.Option(..., "--style", help="Style id (see `beat styles`)."),
    bpm: Optional[float] = typer.Option(None, "--bpm", help="Override the style's tempo."),
    key: Optional[str] = typer.Option(None, "--key", help="Override the key, e.g. Em or 'E minor'."),
    structure: Optional[str] = typer.Option(None, "--structure", help="Bars per section, e.g. intro:4,verse:16,hook:8."),
    add: Optional[str] = typer.Option(None, "--add", help="Extra global styles, comma-separated."),
    avoid: Optional[str] = typer.Option(None, "--avoid", help="Extra things to avoid, comma-separated."),
    styles: Optional[list[Path]] = typer.Option(None, "--styles", "-s", help="Extra styles YAML (repeatable)."),
    from_api: bool = typer.Option(False, "--from-api", help="Ask the provider to draft the plan from the brief instead (still free)."),
    model: str = typer.Option("music_v1", "--model", help="music_v1 | music_v2 | music_v2_5 (--from-api only)."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Save the plan JSON here."),
    as_json: bool = typer.Option(False, "--json", help="Print the plan as JSON."),
):
    """🧮 The arrangement a render would use — section by section, in seconds. Spends nothing."""
    from navig_audio.beats.plan import build_plan, plan_seconds, prompt_text

    catalog = _styles_or_exit(styles)
    s = _style_or_exit(catalog, style)
    kwargs = dict(bpm=bpm, key=key, structure=structure,
                  extra_positive=_split_csv(add), extra_negative=_split_csv(avoid))
    try:
        brief = prompt_text(s, **kwargs)
        plan = build_plan(s, **kwargs)
    except ValueError as exc:
        ch.error("Cannot build the plan", str(exc))
        raise typer.Exit(2) from exc

    if from_api:
        from navig_generate.tools.audio_generation import AudioGenerationError, AudioGenerator

        async def _draft():
            gen = AudioGenerator()
            try:
                return await gen.music_plan(brief, plan_seconds(plan), model_id=model, source_plan=plan)
            finally:
                await gen.close()

        try:
            with ch.create_spinner("Asking the provider for a plan (free)…"):
                plan = asyncio.run(_draft())
        except (AudioGenerationError, ValueError) as exc:
            ch.error("Plan preview failed", str(exc))
            raise typer.Exit(1) from exc

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(_json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")

    if as_json:
        ch.raw_print(_json.dumps(plan, indent=2, ensure_ascii=False))
        return

    ch.dim(f"brief: {brief}")
    table = ch.create_table(columns=[
        {"name": "#", "style": "dim", "justify": "right"},
        {"name": "section", "style": "cyan"},
        {"name": "seconds", "justify": "right"},
        {"name": "local styles"},
    ])
    for i, sec in enumerate(plan.get("sections", []), 1):
        table.add_row(str(i), sec.get("section_name", ""), f"{sec.get('duration_ms', 0) / 1000:.1f}",
                      ", ".join(sec.get("positive_local_styles", [])))
    ch.print_table(table)
    ch.dim("global: " + ", ".join(plan.get("positive_global_styles", [])))
    ch.dim("avoid:  " + ", ".join(plan.get("negative_global_styles", [])))
    ch.success(f"{plan_seconds(plan):.1f} s total" + (f" → {out}" if out else ""))


@beat_app.command("gen")
def beat_gen(
    style: str = typer.Option(..., "--style", help="Style id (see `beat styles`)."),
    count: int = typer.Option(1, "--count", "-n", min=1, max=10, help="How many beats to render."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Folder for the mp3s (default: ~/.navig/audio/beats/<style>)."),
    bpm: Optional[float] = typer.Option(None, "--bpm", help="Override the style's tempo."),
    key: Optional[str] = typer.Option(None, "--key", help="Override the key, e.g. Em or 'E minor'."),
    structure: Optional[str] = typer.Option(None, "--structure", help="Bars per section, e.g. intro:4,verse:16,hook:8."),
    add: Optional[str] = typer.Option(None, "--add", help="Extra global styles, comma-separated."),
    avoid: Optional[str] = typer.Option(None, "--avoid", help="Extra things to avoid, comma-separated."),
    styles: Optional[list[Path]] = typer.Option(None, "--styles", "-s", help="Extra styles YAML (repeatable)."),
    plan_file: Optional[Path] = typer.Option(None, "--plan", help="Render this saved composition plan instead of building one."),
    model: str = typer.Option("music_v1", "--model", help="music_v1 | music_v2 | music_v2_5."),
    seed: Optional[int] = typer.Option(None, "--seed", help="Seed for the first render; each further one adds 1."),
    prompt_mode: bool = typer.Option(False, "--prompt-mode", help="Send the brief as free text (no bar-exact sections)."),
    measure: bool = typer.Option(True, "--measure/--no-measure", help="Measure tempo and key of each render."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the request and stop. Spends nothing."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable result."),
):
    """🎛  Render beats from a style — bar-exact sections, no vocals, tempo and key checked afterwards."""
    from navig_audio.beats.catalog import append_index, make_record, measure as _measure, write_sidecar
    from navig_audio.beats.plan import build_plan, key_name, next_index, plan_seconds, prompt_text, slug

    if model not in _MODELS:
        ch.error(f"Unknown model {model!r}", f"Use one of: {', '.join(_MODELS)}")
        raise typer.Exit(2)

    catalog = _styles_or_exit(styles)
    s = _style_or_exit(catalog, style)
    kwargs = dict(bpm=bpm, key=key, structure=structure,
                  extra_positive=_split_csv(add), extra_negative=_split_csv(avoid))
    try:
        brief = prompt_text(s, **kwargs)
        target_bpm = float(bpm or s.bpm)
        target_key = key_name(key or s.key)
        if plan_file is not None:
            plan = _json.loads(resolve_user_path(plan_file).read_text(encoding="utf-8"))
        else:
            plan = build_plan(s, **kwargs)
    except (ValueError, OSError) as exc:
        ch.error("Cannot build the request", str(exc))
        raise typer.Exit(2) from exc
    seconds = plan_seconds(plan)
    if s.bpm_range and not (s.bpm_range[0] <= target_bpm <= s.bpm_range[1]):
        ch.warning(f"{target_bpm:g} bpm is outside {s.id}'s range {s.bpm_range[0]:g}–{s.bpm_range[1]:g}",
                   "Rendering anyway — the style's drums may not sit right at this tempo.")

    if out is None:
        from navig_sdk.host import media_dir
        out = Path(media_dir("audio")) / "beats" / s.id
    out = resolve_user_path(out)

    if dry_run:
        payload = {
            "style": s.id, "model": model, "count": count, "out": str(out),
            "target": {"bpm": target_bpm, "key": target_key}, "seconds_each": round(seconds, 1),
            "mode": "prompt" if prompt_mode else "composition_plan",
            "prompt": brief, "plan": None if prompt_mode else plan,
        }
        if as_json:
            ch.raw_print(_json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            ch.dim(f"brief: {brief}")
            for i, sec in enumerate(plan.get("sections", []), 1):
                ch.dim(f"  {i:>2}. {sec.get('section_name', '')}: {sec.get('duration_ms', 0) / 1000:.1f}s")
            ch.success(f"Dry run — would render {count} × {seconds:.0f} s of {s.label} at {target_bpm:g} bpm, {target_key}, model {model}",
                       f"into {out}  (nothing was sent)")
        return

    try:
        from navig_generate.tools.audio_generation import (
            AudioGenerationConfig,
            AudioGenerationError,
            AudioGenerator,
            is_audio_generation_available,
        )
    except ImportError as exc:
        ch.error("Audio generation unavailable", str(exc))
        raise typer.Exit(2) from exc
    if not is_audio_generation_available():
        ch.error("No ElevenLabs API key configured",
                 f"Add one:  {key_hint('elevenlabs')}   (or set ELEVENLABS_API_KEY)")
        raise typer.Exit(2)

    out.mkdir(parents=True, exist_ok=True)
    cfg = AudioGenerationConfig.from_env()
    cfg.music_model = model
    cfg.save_locally = False  # we name the files ourselves
    ch.dim(f"Rendering {count} × {seconds:.0f} s · {s.label} · {target_bpm:g} bpm · {target_key} · {model}  — this is the paid step")

    # Numbering continues from what is already in the folder: a second session of three
    # renders gives 04–06, not a second 01 with a suffix.
    first_index = next_index(out, s, bpm=target_bpm, key=target_key)

    async def _run() -> list[dict]:
        gen = AudioGenerator(cfg)
        records: list[dict] = []
        try:
            for i in range(count):
                this_seed = (seed + i) if seed is not None else None
                name = slug(s, bpm=target_bpm, key=target_key, index=first_index + i)
                target = out / f"{name}.mp3"
                n = 1
                while target.exists():  # never overwrite a take (a race, or a hand-named file)
                    n += 1
                    target = out / f"{name}-take{n}.mp3"
                try:
                    if prompt_mode:
                        aud = await gen.generate(brief, kind="music", duration_s=seconds, save=False,
                                                 model_id=model, force_instrumental=True)
                    else:
                        aud = await gen.generate(brief, kind="music", save=False, model_id=model,
                                                 composition_plan=plan, seed=this_seed)
                except AudioGenerationError as exc:
                    records.append({"file": str(target), "error": str(exc), "style": s.id})
                    continue
                if not aud.audio:
                    records.append({"file": str(target), "error": "provider returned no audio", "style": s.id})
                    continue
                target.write_bytes(aud.audio)
                measured = _measure(target, target_bpm=target_bpm) if measure else {}
                rec = make_record(
                    audio_path=target, style_id=s.id, prompt=brief,
                    plan=None if prompt_mode else plan, model=aud.model, seed=this_seed,
                    target_bpm=target_bpm, target_key=target_key,
                    generation_time=aud.generation_time, measured=measured,
                )
                write_sidecar(target, rec)
                append_index(out, rec)
                records.append(rec)
        finally:
            await gen.close()
        return records

    with ch.create_spinner(f"Rendering {s.label}…"):
        try:
            records = asyncio.run(_run())
        except Exception as exc:  # network / provider / quota — surface it, don't swallow
            ch.error("Beat generation failed", str(exc))
            raise typer.Exit(1) from exc

    ok = [r for r in records if not r.get("error") and Path(r["file"]).exists()]
    if as_json:
        ch.raw_print(_json.dumps(records, indent=2, ensure_ascii=False))
        raise typer.Exit(0 if ok else 1)

    table = ch.create_table(columns=[
        {"name": "#", "style": "dim", "justify": "right"},
        {"name": "file", "style": "cyan"},
        {"name": "bpm target → measured", "justify": "right"},
        {"name": "key target → measured"},
        {"name": "loud", "justify": "right"},
    ])
    for i, r in enumerate(records, 1):
        if r.get("error"):
            table.add_row(str(i), Path(r["file"]).name, "[red]— failed[/red]", r["error"][:60], "")
            continue
        m = r.get("measured") or {}
        flag = "" if m.get("bpm_ok", True) else " [yellow]⚠ off[/yellow]"
        bpm_cell = f"{target_bpm:g} → {m.get('bpm_felt', '—')}{flag}" if m else f"{target_bpm:g}"
        key_cell = f"{target_key} → {m.get('key', '—')}" if m else target_key
        loud = f"{m['loudness_dbfs']:.1f} dB" if m.get("loudness_dbfs") is not None else "—"
        table.add_row(str(i), Path(r["file"]).name, bpm_cell, key_cell, loud)
    ch.print_table(table)

    if not ok:
        ch.error("No beat was rendered", "The provider returned no usable audio (check quota, plan tier, or the prompt).")
        raise typer.Exit(1)
    off = [r for r in ok if (r.get("measured") or {}).get("bpm_ok") is False]
    failed = len(records) - len(ok)
    ch.success(
        f"Rendered {len(ok)} beat(s)" + (f"  ({failed} failed)" if failed else "") + (f"  ({len(off)} off-tempo ⚠)" if off else ""),
        f"{out}  · INDEX.md updated · each mp3 has a .json sidecar with its prompt and measurements",
    )


# ── fitting lyrics · layering effects · guide-vocal demos ──────────────────────────


def _read_yaml(path: Path) -> dict:
    import yaml

    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        ch.error(f"Cannot read {Path(path).name}", str(exc))
        raise typer.Exit(2) from exc
    if not isinstance(data, dict):
        ch.error(f"{Path(path).name} must be a mapping")
        raise typer.Exit(2)
    return data


def _beat_bpm(beat: Path, bpm: Optional[float]) -> float:
    """The tempo to lay bars at: explicit, else the render's own target, else measured."""
    if bpm:
        return float(bpm)
    side = beat.with_suffix(".json")
    if side.exists():
        try:
            target = _json.loads(side.read_text(encoding="utf-8")).get("target", {}).get("bpm")
            if target:
                return float(target)
        except (OSError, ValueError):
            pass
    from navig_generate.media.tonality import profile

    return float(profile(beat).bpm_felt)


@beat_app.command("fit")
def beat_fit(
    lyrics: Optional[list[Path]] = typer.Argument(None, help="Lyric files (beat-draft format or plain lyric)."),
    all_dirs: Optional[list[Path]] = typer.Option(None, "--all", help="Fit every .md under this folder (repeatable)."),
    bpm: Optional[float] = typer.Option(None, "--bpm", help="Tempo one line = one bar is counted at."),
    beat: Optional[Path] = typer.Option(None, "--beat", help="A beat mp3: take its tempo and print its bar map."),
    bars_per_line: int = typer.Option(1, "--bars-per-line", min=1, max=4, help="Bars one lyric line spans (2 on a fast beat)."),
    fast: bool = typer.Option(False, "--fast", help="Allow a denser line (fast technique delivery)."),
    report: Optional[Path] = typer.Option(None, "--report", help="Write a markdown report (one row per lyric + the edits)."),
    beats_dirs: Optional[list[Path]] = typer.Option(None, "--beats", help="Beat folder(s): suggest the nearest rendered beat for each text (repeatable)."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable result."),
):
    """📏 Does a lyric sit on the beat? (alias of `navig text lyrics fit`)"""
    try:
        from navig_text.commands.lyrics import lyrics_fit
    except ImportError:
        ch.error("Lyric fitting lives in navig-text now",
                 "Install it: pip install -U navig-text — then `navig text lyrics fit …`")
        raise typer.Exit(2) from None
    if beat is not None and bpm is None:
        bpm = _beat_bpm(resolve_user_path(beat), None)
    lyrics_fit(lyrics=lyrics, all_dirs=all_dirs, bpm=bpm, beat=beat, bars_per_line=bars_per_line,
               fast=fast, report=report, beats_dirs=beats_dirs, as_json=as_json)


@beat_app.command("layer")
def beat_layer(
    beat: Path = typer.Argument(..., help="The dry beat (mp3/wav)."),
    sheet: Path = typer.Option(..., "--sheet", help="Layer sheet YAML: sound effects, where, how loud, fades."),
    bpm: Optional[float] = typer.Option(None, "--bpm", help="Tempo for bar positions (default: the render's target)."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Output file (default: <beat>-layered.mp3)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the placements and stop. Spends nothing."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable result."),
):
    """🎚  Layer sound effects onto a beat — on bars, with fades. The dry beat is never touched."""
    from navig_audio.beats.bars import bar_map
    from navig_audio.beats.layer import LayerError, Master, mix, plan_layers

    beat = resolve_user_path(beat)
    sheet = resolve_user_path(sheet)
    if not beat.exists():
        ch.error("Beat not found", str(beat))
        raise typer.Exit(2)
    data = _read_yaml(sheet)
    tempo = _beat_bpm(beat, bpm or data.get("bpm"))
    with ch.create_spinner("Measuring the beat bar by bar…"):
        bm = bar_map(beat, tempo)
    cache = beat.parent / ".sfx"
    try:
        lp = plan_layers(data, bar_map=bm, cache_dir=cache, base_dir=sheet.parent)
    except (LayerError, ValueError) as exc:
        ch.error("Cannot place the layers", str(exc))
        raise typer.Exit(2) from exc
    m = data.get("master") or {}
    master = Master(fade_in=float(m.get("fade_in", 1.0)), fade_out=float(m.get("fade_out", 3.0)),
                    lufs=m.get("lufs", -14.0))
    dst = resolve_user_path(out) if out else beat.with_name(f"{beat.stem}-layered.mp3")

    placements = [{"name": o.name, "start_s": round(o.start_s, 2), "gain_db": o.gain_db,
                   "fade_in": o.fade_in, "fade_out": o.fade_out,
                   "length_s": round(o.length_s, 2) if o.length_s else None,
                   "source": o.file.name} for o in lp.occurrences]
    if dry_run:
        if as_json:
            ch.raw_print(_json.dumps({
                "beat": str(beat), "bpm": tempo, "out": str(dst),
                "blocks": [b.to_dict() for b in bm.blocks], "placements": placements,
                "to_generate": [{"prompt": p, "duration": d} for p, d, _ in lp.to_generate],
            }, indent=2, ensure_ascii=False))
            return
        for pl in placements:
            ch.dim(f"  {pl['start_s']:>7.2f}s  {pl['name']:<18} {pl['gain_db']:>5.1f} dB  "
                   f"in {pl['fade_in']}s / out {pl['fade_out']}s")
        ch.success(f"Dry run — {len(placements)} placement(s), {len(lp.to_generate)} sound effect(s) to generate",
                   "nothing was sent")
        return

    if lp.to_generate:
        from navig_generate.tools.audio_generation import AudioGenerationError, AudioGenerator

        cache.mkdir(parents=True, exist_ok=True)

        async def _gen():
            gen = AudioGenerator()
            try:
                for prompt, dur, path in lp.to_generate:
                    aud = await gen.generate(prompt, kind="sfx", duration_s=dur, save=False)
                    if not aud.audio:
                        raise AudioGenerationError(f"no audio for sound effect: {prompt[:60]}")
                    path.write_bytes(aud.audio)
            finally:
                await gen.close()

        try:
            with ch.create_spinner(f"Generating {len(lp.to_generate)} sound effect(s)…"):
                asyncio.run(_gen())
        except Exception as exc:  # provider / quota / network — surface, never swallow
            ch.error("Sound-effect generation failed", str(exc))
            raise typer.Exit(1) from exc

    try:
        with ch.create_spinner("Mixing…"):
            mix(beat, lp.occurrences, dst, master, title=f"{beat.stem} (layered)")
    except Exception as exc:  # noqa: BLE001 — ffmpeg errors carry their own message
        ch.error("Mix failed", str(exc))
        raise typer.Exit(1) from exc

    side = beat.with_suffix(".json")
    if side.exists():
        try:
            rec = _json.loads(side.read_text(encoding="utf-8"))
            rec["layered"] = [x for x in rec.get("layered", []) if x.get("file") != dst.name]
            rec["layered"].append({"file": dst.name, "sheet": sheet.name, "placements": placements})
            side.write_text(_json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
        except (OSError, ValueError):
            pass
    if as_json:
        ch.raw_print(_json.dumps({"out": str(dst), "placements": placements}, indent=2, ensure_ascii=False))
        return
    ch.success(f"Layered beat → {dst}",
               f"{len(placements)} placement(s) · dry beat untouched · sound effects cached in {cache}")


@beat_app.command("demo")
def beat_demo(
    beat: Path = typer.Argument(..., help="The beat to demo on."),
    sheet: Path = typer.Option(..., "--sheet", help="Demo sheet YAML: the hook lines, who says each, where it starts."),
    voices: Path = typer.Option(..., "--voices", help="Voices YAML: character → stock voice + pitch + effects."),
    bpm: Optional[float] = typer.Option(None, "--bpm", help="Tempo for bar positions (default: the render's target)."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Output mp3 (default: <beat>-demo-<lang>.mp3)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the placements and stop. Spends nothing."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable result."),
):
    """🎤 A 30-second guide-vocal demo: the hook spoken in character voices on its bars. GUIDE ONLY."""
    from navig_audio.beats.bars import bar_map
    from navig_audio.beats.demo import GUIDE_TAG, DemoError, load_voices, plan_demo, render

    beat = resolve_user_path(beat)
    sheet = resolve_user_path(sheet)
    data = _read_yaml(sheet)
    try:
        vs = load_voices(_read_yaml(resolve_user_path(voices)))
    except DemoError as exc:
        ch.error("Cannot load voices", str(exc))
        raise typer.Exit(2) from exc
    tempo = _beat_bpm(beat, bpm or data.get("bpm"))
    with ch.create_spinner("Measuring the beat bar by bar…"):
        bm = bar_map(beat, tempo)
    cache = beat.parent / ".tts"
    try:
        plan = plan_demo(data, vs, bm, cache)
    except (DemoError, ValueError) as exc:
        ch.error("Cannot place the lines", str(exc))
        raise typer.Exit(2) from exc
    lang = str(data.get("language", "xx"))
    dst = resolve_user_path(out) if out else beat.with_name(f"{beat.stem}-demo-{lang}.mp3")
    todo = [p for p in plan.placed if not p.clip.exists()]
    chars = sum(len(p.text) for p in todo)

    if dry_run:
        for p in plan.placed:
            ch.dim(f"  bar {p.bar:>3} @ {p.at_s:>6.2f}s  {p.voice:<12} {p.text}")
        ch.success(f"Dry run — {len(plan.placed)} line(s), {chars} characters of speech to generate",
                   f"excerpt {plan.start_s:.1f}s + {plan.length_s:g}s → {dst.name}  (nothing was sent)")
        return

    if todo:
        from navig_generate.tools.audio_generation import AudioGenerationError, AudioGenerator

        cache.mkdir(parents=True, exist_ok=True)

        async def _speak():
            gen = AudioGenerator()
            try:
                for p in todo:
                    v = vs[p.voice]
                    aud = await gen.generate(p.text, kind="tts", voice_id=v.voice_id, save=False,
                                             model_id=v.model, voice_settings=v.settings)
                    if not aud.audio:
                        raise AudioGenerationError(f"no speech for line {p.index + 1}")
                    p.clip.write_bytes(aud.audio)
            finally:
                await gen.close()

        try:
            with ch.create_spinner(f"Speaking {len(todo)} line(s)…"):
                asyncio.run(_speak())
        except Exception as exc:  # provider / quota / voice — surface it
            ch.error("Speech generation failed", str(exc))
            raise typer.Exit(1) from exc

    from navig_generate.media.audio_edit import probe_duration

    for p in plan.placed:
        try:
            p.clip_s = probe_duration(p.clip)
        except Exception:  # noqa: BLE001 — an unreadable length is reported as unknown
            p.clip_s = None
    try:
        with ch.create_spinner("Mixing the demo…"):
            render(beat, plan, vs, dst, title=f"{beat.stem} — {lang} hook ({GUIDE_TAG})")
    except Exception as exc:  # noqa: BLE001
        ch.error("Demo mix failed", str(exc))
        raise typer.Exit(1) from exc

    record = {
        "file": dst.name, "beat": beat.name, "language": lang, "tag": GUIDE_TAG,
        "bpm": tempo, "excerpt_start_s": round(plan.start_s, 3), "length_s": plan.length_s,
        "lines": [p.to_dict(bm.bar_s) for p in plan.placed],
        "voices": {k: {"label": v.label, "pitch": v.pitch, "formant": v.formant, "fx": v.fx}
                   for k, v in vs.items() if any(p.voice == k for p in plan.placed)},
    }
    dst.with_suffix(".json").write_text(_json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    over = [ln for ln in record["lines"] if ln.get("overruns_bar")]
    if as_json:
        ch.raw_print(_json.dumps(record, indent=2, ensure_ascii=False))
        return
    ch.success(f"Guide demo → {dst}",
               f"{len(plan.placed)} line(s)"
               + (f" · {len(over)} spoken line(s) run longer than their bar" if over else "")
               + f" · {GUIDE_TAG}")
