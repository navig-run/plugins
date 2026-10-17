"""Speak text and transcribe audio: ``navig voice`` inside navig, ``navig-audio voice`` on its own.

The TTS/STT engines live in this package (``navig_audio.voice``); navig mounts this Typer app on
``navig voice``, and the ``navig-audio`` console script adds it as its ``voice`` group.

No ``from __future__ import annotations`` here: navig's command manifest reads option types
from the live annotations, and a string ``"bool"`` records ``--play/--no-play`` as taking a value.
"""

from pathlib import Path

import typer

from navig_sdk import console as ch
from navig_sdk.host import command_name

_TTS_PROVIDERS_HELP = (
    "TTS Provider (edge, mimo, openai, elevenlabs, openrouter, google_cloud, deepgram)"
)

voice_app = typer.Typer(
    name="voice",
    help="Voice synthesis and recognition commands",
    no_args_is_help=True,
)


@voice_app.command("speak")
def speak_command(
    text: str = typer.Argument(..., help="Text to speak"),
    provider: str = typer.Option(
        None, "--provider", "-p", help=_TTS_PROVIDERS_HELP
    ),
    voice: str = typer.Option(None, "--voice", "-v", help="Voice ID/Name"),
    output: Path | None = typer.Option(None, "--output", "-o", help="Output file path (.mp3)"),
    play: bool = typer.Option(True, "--play/--no-play", help="Play audio immediately"),
    lang: str | None = typer.Option(
        None,
        "--lang",
        "-l",
        help="Language the line is spoken in (en/fr/ru): names are pronounced "
        "per its lexicon (SCHEMA → Skeema / schéma / Схема).",
    ),
    style: str | None = typer.Option(
        None,
        "--style",
        help="Delivery note for providers that take one (MiMo), e.g. "
        "'a warm, wry city pigeon'.",
    ),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Voice profile (a speech preset id, e.g. speech/russian-deep-narrator). "
        "--provider / --voice / --lang / --style override its fields. "
        "List them: navig presets list --tool speech",
    ),
):
    """Synthesize speech from text."""
    import asyncio

    from navig_audio.voice.tts import TTSProvider, get_tts

    plan = None
    if profile:
        # Only the profile path builds a TTS itself. The engines' callers stub
        # `navig_audio.voice.tts` with get_tts/TTSProvider (the documented seam), so these
        # two must not be imported unconditionally here — see the exit-honesty tests.
        from navig_audio.voice.tts import TTS, TTSConfig

        from navig_audio.voice.profiles import apply_pronounce, load_profile

        try:
            name, plan = load_profile(profile)
        except LookupError as exc:
            ch.error(str(exc))
            raise typer.Exit(2) from exc
        except ImportError as exc:
            ch.error("Voice profiles need NAVIG (navig presets)")
            raise typer.Exit(1) from exc
        ch.dim(f"profile: {name}" + (f" · ignored here: {', '.join(plan.ignored)}" if plan.ignored else ""))
        if plan.pronounce:
            text = apply_pronounce(text, plan.pronounce)

    async def _run():
        # A profile is an explicit config (speed, pitch, model…); otherwise the default engine.
        tts = TTS(TTSConfig(**plan.config)) if plan else get_tts()

        # Resolve provider
        prov_enum = plan.provider if plan else None
        if provider:
            try:
                prov_enum = TTSProvider(provider.lower())
            except ValueError:
                ch.warning(f"Unknown provider '{provider}', using default")

        ch.info(f"Synthesizing: '{text}'...")

        extra = {}
        if lang or (plan and plan.lang):
            extra["lang"] = lang or plan.lang
        if style or (plan and plan.kwargs.get("style")):
            extra["style"] = style or plan.kwargs["style"]
        use_voice = voice or (plan.voice if plan and plan.provider in (None, prov_enum) else None)
        result = await tts.synthesize(
            text, provider=prov_enum, voice=use_voice, output_path=output, **extra
        )

        if result.success:
            ch.success(f"Audio saved to: {result.audio_path}")
            if result.provider:
                ch.info(f"Provider: {result.provider.value}, Voice: {result.voice}")

            if play and result.audio_path:
                try:
                    from navig_audio.voice.playback import play_sound

                    success = await play_sound(result.audio_path)
                    if not success:
                        ch.warning(
                            "Playback failed (check audio device or ffmpeg/mpv installation)"
                        )
                except Exception as e:
                    ch.warning(f"Playback error: {e}")
        else:
            # `navig voice speak "x" -o out.mp3 && upload out.mp3` used to upload a
            # file that was never written. typer.Exit propagates out of asyncio.run.
            ch.error(f"TTS Failed: {result.error}")
            raise typer.Exit(1)

    asyncio.run(_run())


@voice_app.command("transcribe")
def transcribe_command(
    file: Path = typer.Argument(..., help="Audio file path"),
    provider: str = typer.Option(None, "--provider", "-p", help="STT Provider"),
):
    """Transcribe audio file to text."""
    import asyncio

    from navig_audio.voice.stt import STTProvider, get_stt

    async def _run():
        stt = get_stt()

        prov_enum = None
        if provider:
            try:
                prov_enum = STTProvider(provider.lower())
            except ValueError as exc:
                ch.error(f"Unknown provider: {provider}")
                raise typer.Exit(2) from exc  # bad input

        ch.info(f"Transcribing {file}...")
        result = await stt.transcribe(file, provider=prov_enum)

        if result.success:
            ch.success("Transcription complete:")
            ch.console.print(f"[bold]{result.text}[/bold]")
            if result.confidence:
                ch.dim(f"Confidence: {result.confidence:.2f}")
        else:
            ch.error(f"Transcription failed: {result.error}")
            raise typer.Exit(1)

    asyncio.run(_run())


@voice_app.command("list-voices")
def list_voices(
    provider: str = typer.Argument("openai", help=_TTS_PROVIDERS_HELP),
):
    """List available voices for a provider."""
    import asyncio

    from navig_audio.voice.tts import TTSProvider, get_tts

    async def _run():
        tts = get_tts()
        try:
            prov_enum = TTSProvider(provider.lower())
        except ValueError as exc:
            ch.error(f"Unknown provider: {provider}")
            raise typer.Exit(2) from exc

        voices = await tts.list_voices(prov_enum)

        if not voices:
            ch.warning(f"No voices returned for '{provider}'.")
            ch.info(f"  If that is unexpected, check its key: {command_name('vault', 'navig-vault')} list")
            return

        from rich.table import Table

        table = Table(title=f"Voices for {provider}")
        table.add_column("ID", style="cyan")
        table.add_column("Name", style="green")
        table.add_column("Language", style="yellow")

        for v in voices:
            table.add_row(v["id"], v["name"], v["language"])

        ch.console.print(table)

    asyncio.run(_run())


_POLICY_HELP = {
    "auto": "free voices; a paid one only when you name it (or a profile does)",
    "free": "never a paid voice, even when named",
    "premium": "your keyed paid voice first, within the daily budget",
    "offline": "never send text off this machine (needs an on-device voice)",
}


def _cm():
    try:
        from navig.config import get_config_manager
    except ImportError as exc:  # navig-audio on its own has no NAVIG config
        ch.error("The voice policy lives in NAVIG's config — run this as `navig voice …`")
        raise typer.Exit(1) from exc
    return get_config_manager()


def _settings() -> dict:
    from navig.core.coerce import coerce_bool, coerce_int

    cm = _cm()
    pol = str(cm.get("voice.policy", "auto") or "auto").lower()
    return {
        "policy": pol if pol in _POLICY_HELP else "auto",
        "daily_char_budget": coerce_int(cm.get("voice.daily_char_budget", 20000), 20000, minimum=0),
        "paid_fallback": coerce_bool(cm.get("voice.paid_fallback", False), default=False),
    }


@voice_app.command("usage")
def usage_command(
    days: int = typer.Option(7, "--days", "-d", min=1, max=62, help="How many days back."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable result."),
):
    """Paid read-aloud characters per day, against the daily budget."""
    import json as _json

    from navig_audio.voice.usage import recent, spent_today

    s = _settings()
    spent = spent_today()
    rows = recent(days)
    if as_json:
        ch.raw_print(_json.dumps({**s, "paid_chars_today": spent, "recent": rows}, indent=2))
        return

    from rich.table import Table

    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("Day", no_wrap=True)
    table.add_column("Characters", justify="right", no_wrap=True)
    table.add_column("Requests", justify="right", no_wrap=True)
    table.add_column("≈ USD", justify="right", no_wrap=True)
    table.add_column("Providers")
    for r in rows:
        table.add_row(
            r["day"], f"{r['chars']:,}", str(r["requests"]), f"{r['usd']:.4f}" if r["usd"] else "[dim]—[/dim]",
            ", ".join(f"{p} {v['chars']:,}" for p, v in sorted(r["providers"].items())),
        )
    budget = s["daily_char_budget"]
    ch.console.print(
        f"Policy [bold]{s['policy']}[/bold] · today {spent:,} of {budget:,} paid characters"
        + (" · [yellow]paid voices off[/yellow]" if budget == 0 else "")
        + (" · paid fallback [green]on[/green]" if s["paid_fallback"] else "")
    )
    if rows:
        ch.console.print(table)
    else:
        ch.dim("No paid speech recorded — free voices cost nothing and are not listed.")
    ch.dim("Change: navig voice policy <auto|free|premium|offline> · navig voice budget <chars>")


@voice_app.command("policy")
def policy_command(
    policy: str | None = typer.Argument(None, help="auto · free · premium · offline (omit to show)"),
    paid_fallback: bool | None = typer.Option(
        None, "--paid-fallback/--no-paid-fallback",
        help="Let Auto retry a failed free voice on your keyed paid one (within the budget).",
    ),
):
    """Show or set which voices read-aloud may use."""
    cm = _cm()
    if policy is not None:
        pol = policy.strip().lower()
        if pol not in _POLICY_HELP:
            ch.error(f"Unknown policy {policy!r}", "use one of: " + ", ".join(_POLICY_HELP))
            raise typer.Exit(2)
        cm.set_global("voice.policy", pol)
    if paid_fallback is not None:
        cm.set_global("voice.paid_fallback", paid_fallback)
    s = _settings()
    from rich.table import Table

    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("", no_wrap=True)
    table.add_column("Policy", no_wrap=True)
    table.add_column("What it means")
    for name, meaning in _POLICY_HELP.items():
        on = name == s["policy"]
        table.add_row("[green]●[/green]" if on else "[dim]○[/dim]", f"[bold]{name}[/bold]" if on else name, meaning)
    ch.console.print(table)
    ch.dim(
        f"paid fallback {'on' if s['paid_fallback'] else 'off'} · daily budget "
        f"{s['daily_char_budget']:,} characters · next: navig voice usage"
    )


@voice_app.command("budget")
def budget_command(
    chars: int | None = typer.Argument(None, min=0, max=10_000_000, help="Paid characters per day (0 = no paid voices). Omit to show."),
):
    """Show or set the daily budget for paid read-aloud voices."""
    cm = _cm()
    if chars is not None:
        cm.set_global("voice.daily_char_budget", int(chars))
    s = _settings()
    from navig_audio.voice.usage import spent_today

    spent = spent_today()
    budget = s["daily_char_budget"]
    if budget == 0:
        ch.success("Paid voices are off", "read-aloud uses free voices only (0 characters a day)")
    else:
        ch.success("Daily paid-voice budget", f"{budget:,} characters · {spent:,} used today")
    ch.dim("ElevenLabs bills about $0.08 per 1,000 characters (v2 Multilingual) — see navig voice usage")
