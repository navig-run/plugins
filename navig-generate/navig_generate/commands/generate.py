"""Generate, iterate and process media: ``navig generate`` inside navig, ``navig-generate`` on its own.

One Typer app, two front doors: navig mounts these commands on ``navig generate`` (next to
the analyse/briefing commands that stay in core); the ``navig-generate`` console script runs
this app directly. Variants land in the space's refs library (``<space>/.navig/refs``);
standalone the space is the nearest folder with a ``.navig/``, else the current directory.
"""
from __future__ import annotations

from pathlib import Path

import typer

from navig_sdk import console as ch
from navig_sdk.host import command_name

# The command a user types here: `navig generate` inside navig, `navig-generate` on its own.
CMD = command_name("generate")

generate_app = typer.Typer(
    name="generate",
    help="Generate images, video and audio with your own AI provider keys, then keep, edit or process them.",
    no_args_is_help=True,
)


# ── clickable output helpers ────────────────────────────────────────────────

def _uri(path: str | None) -> str | None:
    """A file:// URI for terminal hyperlinks (clickable in VS Code / modern terminals)."""
    try:
        return Path(path).resolve().as_uri() if path else None
    except Exception:  # noqa: BLE001
        return None


def _print_variants(variants: list[dict]) -> None:
    """Print each variant as a clickable id + path line."""
    from rich.text import Text  # noqa: PLC0415
    for v in variants:
        uri = _uri(v.get("path"))
        line = Text("  ")
        line.append(v["id"][:8], style=(f"link {uri}" if uri else "bold cyan"))
        line.append(f"  {v.get('provider', '')}  ", style="dim")
        line.append(v.get("path") or "", style=(f"link {uri}" if uri else "dim"))
        ch.console.print(line)


# ── generate + iterate + process (delegate to navig_generate.media.generation_service) ─

@generate_app.command("gen")
def media_gen(
    prompt: str = typer.Argument(..., help="What to generate"),
    modality: str = typer.Option("image", "--modality", help="image | video | audio"),
    provider: str | None = typer.Option(None, "--provider", help="Provider id (omit = default)"),
    placement: str | None = typer.Option(None, "--placement", help="Where it goes, e.g. 'left box of login card'"),
    ref: str | None = typer.Option(None, "--ref", help="Reference screenshot path (recorded as context)"),
    size: str | None = typer.Option(None, "--size", help="Image size, e.g. 1024x1024"),
    kind: str = typer.Option("music", "--kind", help="Audio kind: music | sfx | tts"),
    n: int = typer.Option(1, "--n", help="How many variants (image)"),
) -> None:
    """Generate variant(s) into the active space's refs library."""
    import asyncio  # noqa: PLC0415

    from navig_generate.media import generation_service as gen  # noqa: PLC0415
    label = f"Generating {n} {modality}{'s' if n > 1 else ''} via {provider or 'default provider'}…"
    try:
        with ch.create_spinner(label):
            res = asyncio.run(gen.generate(
                modality=modality, prompt=prompt, provider=provider, placement=placement,
                reference_ref=ref, size=size, kind=kind, n=n,
                space_dir=gen.cwd_space_dir(),
            ))
    except Exception as exc:  # noqa: BLE001
        ch.error(f"Generation failed: {exc}")
        if "not configured" in str(exc):
            ch.dim(_key_hint(modality))
        raise typer.Exit(1) from exc
    # Plan/credit facts BEFORE the results — they explain a failure about to happen.
    for warning in res.get("preflight") or []:
        ch.warning(warning)
    variants = res.get("variants") or []
    if not variants:
        ch.warning("No variants produced (check provider key + quota).")
        raise typer.Exit(1)
    ch.success(f"Generated {len(variants)} variant(s) · group {res['group_id'][:8]}")
    _print_variants(variants)
    ch.dim(f"Keep one:  {CMD} keep <id>   ·   sprite it:  {CMD} process <id> --ops \"...\"")


def _full_id(ref: str) -> str:
    """A variant id from the short id `list` and `gen` print (any unique prefix works)."""
    from navig_media_core.generated_media import get_generated_media  # noqa: PLC0415

    store = get_generated_media()
    if store.get(ref) is not None:
        return ref
    found = store.ids_with_prefix(ref.strip())
    if len(found) == 1:
        return found[0]
    if found:
        ch.error(f"'{ref}' matches more than one variant — give a few more characters.")
    else:
        ch.error(f"No variant with id '{ref}'.", f"See the ids with: {CMD} list --all")
    raise typer.Exit(1)


def _key_hint(modality: str) -> str:
    """Which keys the providers for *modality* read, and which one is already set."""
    from navig_media_core.tools.media_providers import MEDIA_CATALOG, key_status  # noqa: PLC0415

    entries = MEDIA_CATALOG.get(modality, [])
    # "local" counts as always available, but it needs a local server you configured.
    ready = [e["id"] for e in entries if e["id"] != "local" and key_status(e)]
    envs = sorted({v for e in entries for v in e.get("env", [])})
    if ready:
        return f"A key is set for: {', '.join(ready)}. Use it with --provider {ready[0]}."
    return ("Set one provider key in the environment (or the navig vault): "
            + ", ".join(envs) + ". Then pick it with --provider <id>.")


@generate_app.command("list")
def media_list(
    modality: str | None = typer.Option(None, "--modality", help="image | video | audio"),
    status: str | None = typer.Option(None, "--status", help="generated | kept | rejected"),
    all_spaces: bool = typer.Option(False, "--all", "-a",
                                    help="Every space (default: only THIS space's generations)"),
) -> None:
    """List variants in THIS space, newest first (clickable). --all spans every space."""
    from rich.table import Table  # noqa: PLC0415
    from rich.text import Text  # noqa: PLC0415

    from navig_generate.media import generation_service as gen  # noqa: PLC0415

    space_dir = gen.cwd_space_dir()
    rows = gen.history(modality=modality, status=status,
                       space=str(space_dir), all_spaces=all_spaces, limit=100)
    scope = "all spaces" if all_spaces else (Path(space_dir).name or "current dir")

    if not rows:
        ch.info(f"No generations in {scope} yet.")
        if not all_spaces:
            ch.dim(f"See every space with:  {CMD} list --all")
        return

    t = Table(show_header=True, header_style="bold", box=None, pad_edge=False, expand=False)
    t.add_column("#", no_wrap=True)
    t.add_column("status", no_wrap=True)
    t.add_column("type", no_wrap=True)
    t.add_column("provider", no_wrap=True)
    if all_spaces:
        t.add_column("space", no_wrap=True, style="dim")
    t.add_column("prompt", overflow="ellipsis", max_width=48)

    st_style = {"kept": "green", "generated": "yellow", "rejected": "dim"}
    for v in rows:
        uri = _uri(v.get("path"))
        idc = Text(v["id"][:8], style=(f"link {uri}" if uri else "bold cyan"))
        cells = [idc, Text(v["status"], style=st_style.get(v["status"], "")),
                 v["modality"], v.get("provider", "")]
        if all_spaces:
            cells.append(Path(v["space"]).name if v.get("space") else "—")
        cells.append(Text(v.get("prompt") or ""))
        t.add_row(*cells)

    ch.dim(f"media · {scope} · {len(rows)} item(s)")
    ch.print_table(t)
    ch.dim("click an id to open · keep/reject <id>" + ("" if all_spaces else " · --all spans every space"))


@generate_app.command("ingest")
def media_ingest(
    paths: list[str] = typer.Argument(..., help="Dir of externally-generated files, or file(s)."),
    provider: str = typer.Option("external", "--provider", help="Where they came from"),
    prompt: str = typer.Option("", "--prompt", help="Shared prompt (or use per-file .json sidecars)"),
    license: str = typer.Option("", "--license", help="License to record"),
) -> None:
    """Pull externally-generated files into the refs library for review (Midjourney, etc.)."""
    from navig_generate.media import generation_service as gen  # noqa: PLC0415
    src: list[str] | str = paths[0] if len(paths) == 1 else list(paths)
    with ch.create_spinner("Ingesting…"):
        res = gen.ingest(src, provider=provider, prompt=prompt, license=license or None,
                         space_dir=gen.cwd_space_dir())
    ch.success(f"Ingested {res['count']} file(s) · group {res['group_id'][:8]}")
    _print_variants(res["variants"])


@generate_app.command("keep")
def media_keep(media_id: str = typer.Argument(..., help="Variant id to keep (promote).")) -> None:
    """Promote a staged variant to the kept library."""
    from navig_generate.media import generation_service as gen  # noqa: PLC0415
    media_id = _full_id(media_id)
    row = gen.keep(media_id)
    if row is None:
        ch.error("No such variant.")
        raise typer.Exit(1)
    ch.success("Kept → refs library")
    _print_variants([row])


@generate_app.command("reject")
def media_reject(media_id: str = typer.Argument(..., help="Variant id to reject (retained).")) -> None:
    """Reject a staged variant (retained in .rejected, never deleted)."""
    from navig_generate.media import generation_service as gen  # noqa: PLC0415
    media_id = _full_id(media_id)
    row = gen.reject(media_id)
    if row is None:
        ch.error("No such variant.")
        raise typer.Exit(1)
    ch.info("Rejected (retained)")
    _print_variants([row])


@generate_app.command("edit")
def media_edit(
    media_id: str = typer.Argument(..., help="Variant id to edit"),
    instruction: str = typer.Argument(..., help="e.g. 'make this more normal, no cape'"),
) -> None:
    """Instruction-edit an image variant (OpenAI gpt-image)."""
    import asyncio  # noqa: PLC0415

    from navig_generate.media import generation_service as gen  # noqa: PLC0415
    media_id = _full_id(media_id)
    with ch.create_spinner("Editing (gpt-image)…"):
        row = asyncio.run(gen.edit(media_id, instruction))
    ch.success("Edited")
    _print_variants([row])


@generate_app.command("rembg")
def media_rembg(media_id: str = typer.Argument(..., help="Variant id")) -> None:
    """Remove the background → transparent PNG (Recraft)."""
    import asyncio  # noqa: PLC0415

    from navig_generate.media import generation_service as gen  # noqa: PLC0415
    media_id = _full_id(media_id)
    with ch.create_spinner("Removing background (Recraft)…"):
        row = asyncio.run(gen.remove_background(media_id))
    ch.success("Background removed → transparent PNG")
    _print_variants([row])


@generate_app.command("redesign")
def media_redesign(
    media_id: str = typer.Argument(..., help="Variant id"),
    prompt: str = typer.Argument(..., help="How to redesign it"),
    strength: float = typer.Option(0.4, "--strength", help="0=subtle .. 1=loose"),
) -> None:
    """Reference-based img2img redesign (Recraft)."""
    import asyncio  # noqa: PLC0415

    from navig_generate.media import generation_service as gen  # noqa: PLC0415
    media_id = _full_id(media_id)
    with ch.create_spinner("Redesigning (Recraft img2img)…"):
        row = asyncio.run(gen.redesign(media_id, prompt, strength=strength))
    ch.success("Redesigned")
    _print_variants([row])


@generate_app.command("process")
def media_process(
    media_id: str = typer.Argument(..., help="Variant id"),
    ops: str = typer.Option(..., "--ops", help="e.g. 'chroma_key:#FF00FF,quantize:16,downscale:64'"),
) -> None:
    """LOCAL pixel pipeline → a game-ready sprite variant (Pillow)."""
    from navig_generate.media import generation_service as gen  # noqa: PLC0415
    media_id = _full_id(media_id)
    with ch.create_spinner("Processing (pixel pipeline)…"):
        row = gen.process(media_id, _parse_ops(ops))
    ch.success("Processed → sprite")
    _print_variants([row])


@generate_app.command("palette")
def media_palette(
    source: str = typer.Argument(..., help="Dir of reference frames"),
    colors: int = typer.Option(16, "--colors", help="Palette size"),
) -> None:
    """Extract a hex palette from reference frames."""
    from navig_generate.media import generation_service as gen  # noqa: PLC0415
    cols = gen.extract_palette(source, colors=colors)
    ch.success(f"Palette ({len(cols)}): " + " ".join(cols))


@generate_app.command("contact-sheet")
def media_contact_sheet(
    group: str = typer.Option("", "--group", help="Group id (default: all kept images)"),
    status: str = typer.Option("", "--status", help="Filter: generated | kept | rejected"),
) -> None:
    """Montage variants into a review contact sheet (Pillow)."""
    from navig_generate.media import generation_service as gen  # noqa: PLC0415
    path = gen.contact_sheet(group_id=group or None, status=status or None,
                             space_dir=str(gen.cwd_space_dir()))
    ch.success(f"Contact sheet → {path}")


@generate_app.command("license")
def media_license(
    media_id: str = typer.Argument(..., help="Variant id"),
    value: str = typer.Argument(..., help="License string, e.g. CC0-1.0"),
) -> None:
    """Record the license for a variant (provenance)."""
    from navig_generate.media import generation_service as gen  # noqa: PLC0415
    media_id = _full_id(media_id)
    row = gen.set_license(media_id, value)
    if row is None:
        ch.error("No such variant.")
        raise typer.Exit(1)
    ch.success(f"License set: {value}")


def _parse_ops(spec: str) -> list[dict]:
    """Parse 'chroma_key:#FF00FF,quantize:16,downscale:64' → op dicts."""
    ops: list[dict] = []
    for token in (t.strip() for t in spec.split(",") if t.strip()):
        name, _, arg = token.partition(":")
        name, arg = name.strip(), arg.strip()
        if name == "chroma_key":
            ops.append({"op": "chroma_key", "color": arg or "#FF00FF"})
        elif name == "quantize":
            ops.append({"op": "quantize", "colors": int(arg or 16)})
        elif name == "downscale":
            ops.append({"op": "downscale", "size": int(arg or 64)})
        elif name in ("crop", "normalize", "remove_solid_bg"):
            ops.append({"op": name})
        else:
            raise typer.BadParameter(f"unknown op: {name}")
    return ops


# ── ElevenLabs account surface ───────────────────────────────────────────────
# `navig generate eleven …` — everything one ElevenLabs subscription covers that
# is NOT promptable generation (that stays `gen --modality … --kind …`): the
# plan itself, the library of what has been made, and the audio tools that take
# an existing file in (isolate · change-voice · stems · transcribe).

eleven_app = typer.Typer(
    name="eleven",
    help=(
        "ElevenLabs: plan & credits, history, voice library, "
        "and audio tools (isolate · change-voice · stems · transcribe)."
    ),
    no_args_is_help=True,
)


def _eleven_out(stem: str, suffix: str) -> Path:
    """Default destination for a derived audio file: under the media dir.

    Takes a NAME STEM, not a path. ``fetch`` builds one from a history item id, and
    an id is a name — never a filesystem location the operator "typed" — so the
    typed-path resolution rule does not apply to it. Only the name's final component
    is used, so nothing in a stem can climb out of the media dir.
    """
    from navig_sdk.host import media_dir  # noqa: PLC0415

    out_dir = Path(media_dir("audio"))
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{Path(stem).name}{suffix}"


def _resolve_out(out: Path | None) -> Path | None:
    """Resolve a typed ``--out`` against where the operator STOOD (not the space the
    CLI chdir'd into). Inverse of the typed-path guard: passing ``out`` through to a
    helper hides it from that guard, so the resolution has to be done by hand here."""
    if out is None:
        return None
    from navig_generate._typed_path import resolve_user_path  # noqa: PLC0415

    return resolve_user_path(str(out))


@eleven_app.command("subscription")
def eleven_subscription(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """Plan, credits and cloning entitlements behind the configured key (free GET)."""
    import asyncio  # noqa: PLC0415

    from navig_generate.tools.audio_generation import AudioGenerator  # noqa: PLC0415

    async def _load() -> dict:
        gen = AudioGenerator()
        try:
            return await gen.subscription()
        finally:
            await gen.close()

    try:
        with ch.create_spinner("Reading ElevenLabs subscription …"):
            data = asyncio.run(_load())
    except Exception as exc:  # noqa: BLE001 — surfaced verbatim; body carries the why
        ch.error(f"Subscription lookup failed: {exc}")
        raise typer.Exit(1) from exc

    if json_out:
        import json  # noqa: PLC0415

        ch.console.print_json(json.dumps(data))
        return

    from rich.table import Table  # noqa: PLC0415

    used, limit = int(data.get("character_count") or 0), int(data.get("character_limit") or 0)
    pct = (used / limit * 100) if limit else 0.0
    t = Table(show_header=False, box=None, pad_edge=False)
    t.add_column(style="dim", no_wrap=True)
    t.add_column("")
    t.add_row("plan", f"{data.get('tier', '?')} · {data.get('status', '?')}")
    t.add_row("characters", f"{used:,} / {limit:,}  ({pct:.0f}% used)")
    t.add_row(
        "voice slots",
        f"{data.get('voice_slots_used', '?')} / {data.get('voice_limit', '?')}"
        f" · professional: {data.get('professional_voice_slots_used', 0)}",
    )
    t.add_row(
        "voice cloning",
        f"instant: {'yes' if data.get('can_use_instant_voice_cloning') else 'no'}"
        f" · professional: {'yes' if data.get('can_use_professional_voice_cloning') else 'no'}",
    )
    reset = data.get("next_character_count_reset_unix")
    if reset:
        from datetime import datetime  # noqa: PLC0415

        t.add_row("resets", datetime.fromtimestamp(int(reset)).strftime("%Y-%m-%d %H:%M"))
    ch.print_table(t)
    ch.dim(
        "library: history · voices   |   tools: isolate · change-voice · stems · transcribe"
    )


@eleven_app.command("history")
def eleven_history(
    limit: int = typer.Option(20, "--limit", "-n", min=1, max=100, help="Items to show"),
    search: str | None = typer.Option(None, "--search", help="Full-text search the API supports"),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """List what this key has already generated (free — GET only, costs no credits)."""
    import asyncio  # noqa: PLC0415
    from datetime import datetime  # noqa: PLC0415

    from rich.table import Table  # noqa: PLC0415

    from navig_generate.tools.media_library import MediaLibrary  # noqa: PLC0415

    async def _load() -> dict:
        lib = MediaLibrary()
        try:
            return await lib.history(page_size=limit, search=search)
        finally:
            await lib.close()

    try:
        with ch.create_spinner("Fetching ElevenLabs history …"):
            data = asyncio.run(_load())
    except Exception as exc:  # noqa: BLE001
        ch.error(f"History lookup failed: {exc}")
        raise typer.Exit(1) from exc

    items = list(data.get("history") or [])
    if json_out:
        import json  # noqa: PLC0415

        ch.console.print_json(json.dumps(items))
        return
    if not items:
        ch.info("No history yet for this key.")
        return

    t = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    t.add_column("when", no_wrap=True)
    t.add_column("model", no_wrap=True)
    t.add_column("voice", no_wrap=True)
    t.add_column("chars", no_wrap=True, justify="right")
    t.add_column("text", overflow="ellipsis", max_width=48)
    t.add_column("id", no_wrap=True, style="dim")
    for it in items:
        when = datetime.fromtimestamp(int(it.get("date_unix") or 0)).strftime("%Y-%m-%d %H:%M")
        delta = it.get("character_count_change_from")
        delta_to = it.get("character_count_change_to")
        chars = f"{(delta_to or 0) - (delta or 0)}" if delta is not None else ""
        t.add_row(
            when,
            str(it.get("model_id") or ""),
            str(it.get("voice_name") or ""),
            chars,
            str(it.get("text") or ""),
            str(it.get("history_item_id") or ""),
        )
    ch.print_table(t)
    ch.dim(f"fetch audio:  {CMD} eleven fetch <id>   ·  {CMD} eleven history --json for ids")


@eleven_app.command("fetch")
def eleven_fetch(
    history_id: str = typer.Argument(..., help="history_item_id from `eleven history`"),
    out: Path | None = typer.Option(None, "--out", "-o", help="Destination file (default: media dir)"),
) -> None:
    """Download one history item's audio (free — the bytes are already stored)."""
    import asyncio  # noqa: PLC0415

    from navig_generate.tools.media_library import MediaLibrary, write_output_file  # noqa: PLC0415

    async def _load() -> bytes:
        lib = MediaLibrary()
        try:
            return await lib.history_audio(history_id)
        finally:
            await lib.close()

    try:
        with ch.create_spinner("Downloading history audio …"):
            audio = asyncio.run(_load())
        dest = write_output_file(audio, _resolve_out(out) or _eleven_out(history_id, ".mp3"),
                                 label="history fetch")
    except Exception as exc:  # noqa: BLE001
        ch.error(f"Fetch failed: {exc}")
        raise typer.Exit(1) from exc
    ch.success(f"Saved → {dest}")


@eleven_app.command("voices")
def eleven_voices(
    search: str | None = typer.Option(None, "--search", "-s", help="Search the public library"),
    language: str | None = typer.Option(None, "--language", help="e.g. en, es, fr"),
    mine: bool = typer.Option(False, "--mine", help="Your own voices (ids for change-voice)"),
    limit: int = typer.Option(20, "--limit", "-n", min=1, max=100),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """Search the voice library — or list your own voices with `--mine` (free GETs)."""
    import asyncio  # noqa: PLC0415

    from rich.table import Table  # noqa: PLC0415

    from navig_generate.tools.audio_generation import AudioGenerator  # noqa: PLC0415
    from navig_generate.tools.media_library import MediaLibrary  # noqa: PLC0415

    async def _shared() -> list[dict]:
        lib = MediaLibrary()
        try:
            data = await lib.shared_voices(search=search, language=language, page_size=limit)
            return list(data.get("voices") or [])
        finally:
            await lib.close()

    async def _own() -> list[dict]:
        gen = AudioGenerator()
        try:
            voices = await gen.list_voices()
        finally:
            await gen.close()
        # The account's own voices come back unfiltered from the API — apply the
        # same --search / --language / --limit the shared-library path gets, so
        # `--mine -n 5 -s alice` does not silently list everything.
        if search:
            needle = search.lower()
            voices = [v for v in voices if needle in str(v.get("name") or "").lower()]
        if language:
            prefix = language.lower()
            voices = [
                v for v in voices
                if str(v.get("language") or v.get("locale") or "").lower().startswith(prefix)
            ]
        return voices[:limit]

    try:
        with ch.create_spinner("Fetching voices …"):
            voices = asyncio.run(_own() if mine else _shared())
    except Exception as exc:  # noqa: BLE001
        ch.error(f"Voice lookup failed: {exc}")
        raise typer.Exit(1) from exc

    if json_out:
        import json  # noqa: PLC0415

        ch.console.print_json(json.dumps(voices))
        return
    if not voices:
        ch.info("No voices matched.")
        return

    t = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    t.add_column("name", no_wrap=True)
    t.add_column("id", no_wrap=True, style="dim")
    t.add_column("language", no_wrap=True)
    if not mine:
        t.add_column("accent", no_wrap=True)
        t.add_column("gender", no_wrap=True)
        t.add_column("use", overflow="ellipsis", max_width=32)
    for v in voices:
        row = [str(v.get("name") or ""), str(v.get("voice_id") or ""),
               str(v.get("language") or v.get("locale") or "")]
        if not mine:
            row += [str(v.get("accent") or ""), str(v.get("gender") or ""),
                    str(v.get("use_case") or "")]
        t.add_row(*row)
    ch.print_table(t)
    ch.dim(f"use an id:  {CMD} eleven change-voice <file> --voice <id>")


def _eleven_tool_setup(src: Path) -> Path:
    """Validate the input file exists before any spinner starts."""
    from navig_generate._typed_path import resolve_user_path  # noqa: PLC0415

    p = resolve_user_path(str(src)).expanduser()
    if not p.is_file():
        ch.error(f"No such file: {p}")
        raise typer.Exit(1)
    return p


@eleven_app.command("isolate")
def eleven_isolate(
    src: Path = typer.Argument(..., help="Audio/video file to clean (voice isolator)"),
    out: Path | None = typer.Option(None, "--out", "-o", help="Destination audio file"),
) -> None:
    """Remove background noise, keep the voice (costs credits)."""
    import asyncio  # noqa: PLC0415

    from navig_generate.tools.audio_generation import AudioGenerator  # noqa: PLC0415

    p = _eleven_tool_setup(src)

    async def _run() -> bytes:
        gen = AudioGenerator()
        try:
            return await gen.isolate_voice(p)
        finally:
            await gen.close()

    try:
        with ch.create_spinner("Isolating voice …"):
            audio = asyncio.run(_run())
        dest = _resolve_out(out) or _eleven_out(p.stem, "-isolated.mp3")
        from navig_generate.tools.media_library import write_output_file  # noqa: PLC0415

        write_output_file(audio, dest, label="isolate")
    except Exception as exc:  # noqa: BLE001
        ch.error(f"Voice isolation failed: {exc}")
        raise typer.Exit(1) from exc
    ch.success(f"Isolated → {dest}")


@eleven_app.command("change-voice")
def eleven_change_voice(
    src: Path = typer.Argument(..., help="Recording to re-speak"),
    voice: str = typer.Option(..., "--voice", help="Target voice_id (see `eleven voices`)"),
    out: Path | None = typer.Option(None, "--out", "-o", help="Destination audio file"),
    model: str | None = typer.Option(None, "--model", help="speech-to-speech model id"),
    keep_noise: bool = typer.Option(False, "--keep-noise", help="Don't clean the source first"),
) -> None:
    """Re-speak a recording in another voice, keeping timing and delivery (costs credits)."""
    import asyncio  # noqa: PLC0415

    from navig_generate.tools.audio_generation import AudioGenerator  # noqa: PLC0415

    p = _eleven_tool_setup(src)

    async def _run() -> bytes:
        gen = AudioGenerator()
        try:
            return await gen.change_voice(
                p, voice, model_id=model, remove_background_noise=not keep_noise
            )
        finally:
            await gen.close()

    try:
        with ch.create_spinner(f"Re-speaking in {voice} …"):
            audio = asyncio.run(_run())
        dest = _resolve_out(out) or _eleven_out(p.stem, f"-{voice[:8]}.mp3")
        from navig_generate.tools.media_library import write_output_file  # noqa: PLC0415

        write_output_file(audio, dest, label="change-voice")
    except Exception as exc:  # noqa: BLE001
        ch.error(f"Voice change failed: {exc}")
        raise typer.Exit(1) from exc
    ch.success(f"Re-spoken → {dest}")


@eleven_app.command("stems")
def eleven_stems(
    src: Path = typer.Argument(..., help="Track to split"),
    out: Path | None = typer.Option(None, "--out", "-o", help="Destination .zip"),
    variation: str = typer.Option(
        "two_stems_v1", "--variation", help="two_stems_v1 (vocals+instrumental) | six_stems_v1"
    ),
    c2pa: bool = typer.Option(False, "--c2pa", help="Sign the output with C2PA metadata"),
) -> None:
    """Split a track into stems — returns a zip (costs credits; slow on long files)."""
    import asyncio  # noqa: PLC0415

    from navig_generate.tools.audio_generation import AudioGenerator  # noqa: PLC0415

    p = _eleven_tool_setup(src)

    async def _run() -> bytes:
        gen = AudioGenerator()
        try:
            return await gen.separate_stems(
                p, stem_variation_id=variation, sign_with_c2pa=c2pa
            )
        finally:
            await gen.close()

    try:
        with ch.create_spinner("Separating stems …"):
            blob = asyncio.run(_run())
        dest = _resolve_out(out) or _eleven_out(p.stem, "-stems.zip")
        from navig_generate.tools.media_library import write_output_file  # noqa: PLC0415

        write_output_file(blob, dest, label="stems")
    except Exception as exc:  # noqa: BLE001
        ch.error(f"Stem separation failed: {exc}")
        raise typer.Exit(1) from exc
    ch.success(f"Stems → {dest}")


@eleven_app.command("transcribe")
def eleven_transcribe(
    src: Path = typer.Argument(..., help="Audio/video file to transcribe"),
    language: str | None = typer.Option(None, "--language", help="Force a language code"),
    speakers: int | None = typer.Option(None, "--speakers", help="Expected number of speakers"),
    no_diarize: bool = typer.Option(False, "--no-diarize", help="Skip speaker attribution"),
    json_out: bool = typer.Option(False, "--json", help="Full payload incl. word timings"),
) -> None:
    """Transcribe with ElevenLabs Scribe (costs credits; word timings included)."""
    import asyncio  # noqa: PLC0415
    import json  # noqa: PLC0415

    from navig_generate.tools.audio_generation import AudioGenerator  # noqa: PLC0415

    p = _eleven_tool_setup(src)

    async def _run() -> dict:
        gen = AudioGenerator()
        try:
            return await gen.transcribe(
                p,
                language_code=language,
                diarize=not no_diarize,
                num_speakers=speakers,
            )
        finally:
            await gen.close()

    try:
        with ch.create_spinner("Transcribing …"):
            data = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        ch.error(f"Transcription failed: {exc}")
        raise typer.Exit(1) from exc

    if json_out:
        ch.console.print_json(json.dumps(data))
        return
    words = data.get("words") or []
    ch.console.print(str(data.get("text") or "").strip())
    ch.dim(
        f"language {data.get('language_code', '?')} · {len(words)} word timing(s) · "
        f"--json for the full payload"
    )


generate_app.add_typer(eleven_app, name="eleven")


# ── the channel's statement overlay (ported from the workspace's statement.py) ──

@generate_app.command("statement")
def generate_statement(
    spec: Path = typer.Argument(..., help="Spec JSON: line (*red* segments), sub, tag, size, coords/wave/clock, grid/hud/mark/bip."),
    out: Path = typer.Argument(..., help="Where to write the PNG (1080x1920 RGBA)."),
    workspace: Path | None = typer.Option(None, "--workspace", "-C", help="Channel workspace (default: NAVIG_WORKSPACE or the channel directory)."),
    mark: Path | None = typer.Option(None, "--mark", help="Mark overlay (default: <workspace>/mark.png)."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable result."),
) -> None:
    """⬛ Render a STATEMENT overlay — one line, in Schema's own interface language.

    Druk carries the statement, Fira Mono the readout; corner brackets frame it
    as something looked AT through an instrument. `*red word*` segments in the
    spec alternate the two word colours. Byte-parity with the channel's own
    renderer: same fonts, same mark, same constants.
    """
    import json as _json  # noqa: PLC0415

    from navig_generate._typed_path import resolve_user_path  # noqa: PLC0415
    from navig_generate.statement import (  # noqa: PLC0415
        missing_fonts,
        prepare_line,
        render,
    )
    from navig_generate.workspace import default_workspace  # noqa: PLC0415

    spec_path, out_path = resolve_user_path(spec), resolve_user_path(out)
    try:
        data = _json.loads(spec_path.read_text(encoding="utf-8"))
    except OSError as exc:
        ch.error("Spec not readable", str(exc))
        raise typer.Exit(2) from exc
    except ValueError as exc:
        ch.error("Spec is not valid JSON", f"{spec_path}: {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(data, dict):
        ch.error("Spec must be a JSON object", f"{spec_path} holds {type(data).__name__}")
        raise typer.Exit(2)
    font_warnings = missing_fonts()
    if "Inter-SemiBold.ttf" in font_warnings:
        ch.error(
            "Fallback font missing",
            "Inter-SemiBold.ttf will not load — every face falls back to it, "
            "so nothing can render; install it to C:/Windows/Fonts",
        )
        raise typer.Exit(1)
    if font_warnings and not json_out:
        ch.warning(
            "Font(s) missing",
            f"{', '.join(font_warnings)} fall back to Inter-SemiBold — "
            "output will NOT byte-match the channel's renderer",
        )
    if mark is not None:
        mark_path: Path | None = resolve_user_path(mark)
    else:
        mark_path = default_workspace(workspace) / "mark.png"
    try:
        path = render(prepare_line(data), out_path, mark=mark_path)
    except OSError as exc:
        ch.error("Render failed", str(exc))
        raise typer.Exit(1) from exc
    if json_out:
        ch.console.print_json(
            _json.dumps({"out": str(path), "size": [1080, 1920], "font_warnings": font_warnings})
        )
        return
    ch.success("Statement rendered", str(path))
    ch.raw_print(str(path))


# ── style templates — the owner's inspiration catalogue ──────────────────────
# Source of truth: wave1/STYLE-TEMPLATES.md + styles.json (extracted 2026-10-14
# from the owner's image library: cxema/art, cyberwizard, generated/chatgpt,
# chat attachments). `navig generate style list|show` reads the bundled copy.

style_app = typer.Typer(
    name="style",
    help="Schema style templates: extracted prompt / motion / layout recipes from the owner's inspiration library.",
    no_args_is_help=True,
)
generate_app.add_typer(style_app, name="style")


def _load_styles() -> dict:
    import json as _json  # noqa: PLC0415
    cat = Path(__file__).resolve().parent.parent / "styles.json"
    return _json.loads(cat.read_text(encoding="utf-8"))


@style_app.command("list")
def style_list(
    kind: str = typer.Option(None, "--kind", help="image | layout | archive"),
) -> None:
    """List every template id with its kind and title."""
    styles = _load_styles()["styles"]
    for s in styles:
        if kind and s.get("kind") != kind:
            continue
        ch.console.print(f"  {s['id']:8}  {s.get('kind', ''):7}  {s['title']}")
    ch.console.print(f"  {len(styles)} templates — show one with: {CMD} style show <id>")


@style_app.command("show")
def style_show(
    style_id: str = typer.Argument(..., help="Template id, e.g. INS-03 or LAY-02"),
) -> None:
    """Print one template: source, prompt, motion (side effects), layout."""
    data = _load_styles()
    match = next(
        (s for s in data["styles"] if s["id"].lower() == style_id.lower()), None
    )
    if match is None:
        ch.error(f"Unknown style '{style_id}'", f"run `{CMD} style list` to see ids")
        raise typer.Exit(1)
    ch.console.print(f"{match['id']} - {match['title']}")
    for key in ("kind", "source", "prompt", "motion", "layout"):
        if match.get(key):
            ch.console.print(f"{key}: {match[key]}")
