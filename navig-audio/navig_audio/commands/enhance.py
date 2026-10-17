"""``navig audio enhance`` — restore and master an existing recording, verified.

  navig audio enhance take.wav --analyze             what's measurably wrong (nothing is written)
  navig audio enhance take.wav                       Preserve Original: fix only measured defects
  navig audio enhance take.wav --mode studio --format wav,flac
  navig audio enhance take.wav --preset audio-enhance/voice-restoration --out D:\\clean
  navig audio enhance take.wav --mode custom --set denoise=strong --set eq=gentle
  navig audio enhance-presets                        the enhancement presets

The original is never modified. Every output is verified against it (events, timing,
pitch, tone, peaks); a candidate that fails is retried with a safer plan, then
rejected, and nothing but a report is kept. Mounted on ``audio_app`` by import side
effect from ``audio.py``.
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from typing import List, Optional

import typer

from navig_audio._typed_path import resolve_user_path
from navig_audio.commands.audio import audio_app
from navig_sdk import console as ch

_SEV = {"major": "[red]● major[/red]", "minor": "[yellow]● minor[/yellow]", "info": "[dim]○ info[/dim]"}
_VERDICT = {
    "pass": "[green]✓ verified — same recording[/green]",
    "warn": "[yellow]! creative or partial — compare by ear[/yellow]",
    "fail": "[red]✗ rejected — the original is unchanged[/red]",
    "clean": "[green]✓ already clean — nothing to fix[/green]",
    "planned": "[cyan]○ plan only (dry run)[/cyan]",
}


def _parse_sets(pairs: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            ch.error(f"--set expects key=value, got {pair!r}")
            raise typer.Exit(2)
        k, v = pair.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _typed_params(raw: dict[str, str]) -> dict:
    """Validate --set values against the audio.enhance schema (navig), or pass them through."""
    if not raw:
        return {}
    try:
        from navig.presets import PresetError
        from navig.presets.model import validate_params
    except ImportError:
        return raw
    try:
        return validate_params("audio.enhance", raw)
    except PresetError as exc:
        ch.error("Bad setting", str(exc))
        raise typer.Exit(2) from None


def _findings_table(report) -> None:
    table = ch.create_table(columns=[
        {"name": "", "no_wrap": True},
        {"name": "finding", "no_wrap": True},
        {"name": "evidence"},
    ])
    for f in report.findings:
        table.add_row(_SEV.get(f.severity, f.severity), f.title, f.evidence)
    if report.findings:
        ch.print_table(table)
    ch.dim(
        f"{report.codec} · {report.sample_rate} Hz · {report.channels} ch · {report.decoded_duration_s:.3f}s"
        f" · {report.lufs:.1f} LUFS · TP {report.true_peak_db:+.1f} dBTP · {len(report.onsets_s)} event(s)"
        f" · looks like {report.category} ({report.category_confidence})"
    )


def _plan_table(plan) -> None:
    if plan.empty:
        ch.info("No processing recommended — " + (plan.notes[-1] if plan.notes else "nothing to fix."))
        return
    table = ch.create_table(columns=[
        {"name": "step", "style": "cyan", "no_wrap": True},
        {"name": "where", "no_wrap": True},
        {"name": "why"},
    ])
    for s in plan.stages:
        where = "[yellow]cloud[/yellow]" if not s.local else "local"
        table.add_row(s.name + (" [magenta](creative)[/magenta]" if s.creative else ""), where, s.reason)
    ch.print_table(table)
    for sk in plan.skipped:
        ch.dim(f"skipped {sk['stage']}: {sk['why']}")


@audio_app.command("enhance")
def audio_enhance(
    file: Path = typer.Argument(..., help="The recording to enhance (left untouched)."),
    mode: Optional[str] = typer.Option(None, "--mode", "-m", help="preserve · studio · cinematic · custom"),
    preset: Optional[str] = typer.Option(None, "--preset", "-p", help="An audio.enhance preset id (see `navig audio enhance-presets`)."),
    sets: Optional[List[str]] = typer.Option(None, "--set", help="Override a setting: key=value (repeatable)."),
    analyze_only: bool = typer.Option(False, "--analyze", help="Only measure and show the findings."),
    formats: str = typer.Option("wav", "--format", "-f", help="Comma list: wav (24-bit) · wav16 · flac · mp3"),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Output folder (default: next to the original)."),
    category: str = typer.Option("auto", "--category", help="auto · speech · sfx · music · ambience · mixed"),
    allow_cloud: bool = typer.Option(False, "--allow-cloud", help="Allow a cloud step (uploads the file) if the preset asks for one."),
    force: bool = typer.Option(False, "--force", help="Process even when nothing needs fixing (peak protection only)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan; write nothing."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable result."),
) -> None:
    """🎛 Restore and master a recording — measured, explained, verified, never overwritten."""
    from navig_audio.enhance import EnhanceError, analyze, enhance
    from navig_audio.enhance import presets as presets_mod
    from navig_audio.enhance.analysis import AnalyzeError

    src = resolve_user_path(file)
    if not src.is_file():
        ch.error(f"No such file: {src}", "A relative path resolves against the directory you typed it in.")
        raise typer.Exit(2)

    if analyze_only:
        try:
            report = analyze(src, category=category)
        except AnalyzeError as exc:
            ch.error("Could not analyse the file", str(exc))
            raise typer.Exit(1) from None
        if as_json:
            ch.raw_print(_json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
            return
        _findings_table(report)
        ch.info(f"Next: navig audio enhance \"{file}\" --dry-run   (see what would change)")
        return

    params: dict = {}
    preset_id = preset or (None if mode else presets_mod.default_id())
    if preset_id:
        try:
            params = presets_mod.load(preset_id)
        except KeyError:
            ch.error(f"No enhancement preset {preset_id!r}", "See: navig audio enhance-presets")
            raise typer.Exit(2) from None
    params.update(_typed_params(_parse_sets(sets)))
    if allow_cloud:
        params["allow_cloud"] = True
    run_mode = mode or str(params.get("mode") or "preserve")
    fmt_list = [f.strip().lower() for f in formats.split(",") if f.strip()]
    out_dir = resolve_user_path(out) if out else None

    def _progress(msg: str, _frac: float) -> None:
        if not as_json:
            ch.dim(f"… {msg}")

    try:
        result = enhance(
            src, mode=run_mode, params=params, out_dir=out_dir, formats=fmt_list, category=category,
            dry_run=dry_run, force=force, preset_id=preset_id, progress=_progress,
        )
    except EnhanceError as exc:
        ch.error("Enhance failed", str(exc))
        raise typer.Exit(1) from None
    except ValueError as exc:
        ch.error("Bad option", str(exc))
        raise typer.Exit(2) from None

    if as_json:
        ch.raw_print(_json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        raise typer.Exit(0 if result.ok else 1)

    _findings_table(result.report)
    _plan_table(result.plan)
    v = result.verification
    if v is not None:
        checks = ch.create_table(columns=[
            {"name": "check", "no_wrap": True},
            {"name": "result", "no_wrap": True},
            {"name": "limit", "no_wrap": True},
            {"name": "meaning"},
        ])
        for c in v.checks:
            mark = "[green]✓[/green]" if c.ok else ("[red]✗[/red]" if c.hard else "[yellow]![/yellow]")
            checks.add_row(f"{mark} {c.name}", str(c.value), str(c.limit), c.detail)
        ch.print_table(checks)
        if len(result.attempts) > 1:
            ch.dim("attempts: " + " → ".join(f"{'+'.join(a.stages) or '∅'} ({a.verdict})" for a in result.attempts))
    ch.console.print(_VERDICT.get(result.verdict, result.verdict))
    for o in result.outputs:
        ch.success(f"Saved {o}")
    if v is not None and result.outputs:
        ch.dim(f"A/B fairly: play the result {v.match_gain_db:+.1f} dB to match the original's loudness.")
    if result.sidecar:
        ch.dim(f"Report: {result.sidecar}")
    if result.verdict == "clean":
        ch.info(f"Next: navig audio enhance \"{file}\" --mode studio   (polish anyway) — the original is the best version")
    elif result.verdict == "fail":
        ch.info("Next: try --mode preserve, or --set denoise=off to keep more of the original")
        raise typer.Exit(1)
    elif result.verdict == "planned":
        ch.info(f"Next: run it without --dry-run to write {', '.join(fmt_list)}")


@audio_app.command("enhance-presets")
def audio_enhance_presets(as_json: bool = typer.Option(False, "--json")) -> None:
    """🎚 The enhancement presets (Preserve Original, Clean SFX, Studio Polish, …)."""
    from navig_audio.enhance import presets as presets_mod

    items = presets_mod.list_presets()
    if as_json:
        ch.raw_print(_json.dumps(items, indent=2, ensure_ascii=False))
        return
    default = presets_mod.default_id()
    table = ch.create_table(columns=[
        {"name": "", "no_wrap": True},
        {"name": "id", "style": "cyan", "no_wrap": True},
        {"name": "name", "no_wrap": True},
        {"name": "mode", "no_wrap": True},
        {"name": "what it does"},
    ])
    for p in items:
        table.add_row("[green]●[/green]" if p["id"] == default else "", p["id"], p["name"],
                      str((p.get("params") or {}).get("mode", "")), p.get("description", ""))
    ch.print_table(table)
    ch.dim("● default · next: navig audio enhance <file> --preset <id>")
