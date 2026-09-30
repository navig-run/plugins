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


# ── generate + iterate + process (delegate to navig.media.generation_service) ────

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
    variants = res.get("variants") or []
    if not variants:
        ch.warning("No variants produced (check provider key + quota).")
        raise typer.Exit(1)
    ch.success(f"Generated {len(variants)} variant(s) · group {res['group_id'][:8]}")
    _print_variants(variants)
    ch.dim(f"Keep one:  {CMD} keep <id>   ·   sprite it:  {CMD} process <id> --ops \"...\"")


def _full_id(ref: str) -> str:
    """A variant id from the short id `list` and `gen` print (any unique prefix works)."""
    from navig_generate.generated_media import get_generated_media  # noqa: PLC0415

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
    from navig_generate.tools.media_providers import MEDIA_CATALOG, key_status  # noqa: PLC0415

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
