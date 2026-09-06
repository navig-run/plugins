"""``navig design`` — AI design edits for HTML elements (agent-backed).

The CLI/agent surface behind the Design Mode browser lens's "Apply with AI".
``navig design edit`` takes one element's HTML + an instruction and prints the
revised element HTML as JSON on stdout, so the lens can parse it out of a
``host.run()`` result. Implemented in navig-text because it is an agent-backed
text/markup transform sharing the same core AI client — no new deps, no new
gateway routes.

  # human use
  navig design edit --prompt "make this a bold dark CTA" --html '<button>Buy</button>'
  # lens use — base64 JSON {html, prompt, selector?, styles?}
  navig design edit --b64 <BASE64>

Output (stdout, single-line JSON):
  {"ok": true, "html": "<button …>…</button>", "model": "…"}
  {"ok": false, "error": "…"}
Nothing here prints a secret.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json as _json
from pathlib import Path
from typing import Optional

import typer

from navig.lazy_loader import lazy_import

ch = lazy_import("navig.console_helper")

design_app = typer.Typer(
    name="design",
    help="🎨 AI design edits + design tokens for the Design Mode lens.",
    no_args_is_help=True,
)

tokens_app = typer.Typer(
    name="tokens",
    help="🎨 Design tokens — persist / show the extracted design system.",
    no_args_is_help=True,
)
design_app.add_typer(tokens_app, name="tokens")


def _emit(payload: dict) -> None:
    """Single-line JSON to stdout via the approved emitter (no Rich wrapping)."""
    ch.emit_json(payload, indent=None)


def _decode_b64(b64: str) -> dict:
    try:
        raw = base64.b64decode(b64.encode("ascii"), validate=True)
        data = _json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise typer.BadParameter(f"--b64 is not valid base64-encoded JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise typer.BadParameter("--b64 JSON must decode to an object")
    return data


@design_app.command("edit")
def design_edit_cmd(
    prompt: Optional[str] = typer.Option(None, "--prompt", "-p", help="The design instruction."),
    html: Optional[str] = typer.Option(None, "--html", help="The element's outer HTML."),
    selector: Optional[str] = typer.Option(None, "--selector", help="CSS selector (context only)."),
    styles: Optional[str] = typer.Option(None, "--styles", help="Current styles summary (context only)."),
    b64: Optional[str] = typer.Option(
        None, "--b64", help="Base64 JSON {html,prompt,selector?,styles?} — used by the lens."
    ),
) -> None:
    """Restyle/rewrite one element per an instruction; print the revised HTML as JSON."""
    if b64:
        data = _decode_b64(b64)
        html = html or data.get("html")
        prompt = prompt or data.get("prompt") or data.get("instruction")
        selector = selector or data.get("selector")
        styles = styles or data.get("styles")

    if not (html and html.strip()) or not (prompt and prompt.strip()):
        _emit({"ok": False, "error": "both an instruction (--prompt) and element HTML (--html/--b64) are required"})
        raise typer.Exit(2)

    try:
        from navig.agent.ai_client import get_ai_client
    except ImportError as exc:  # pragma: no cover - core always present at runtime
        _emit({"ok": False, "error": f"AI client unavailable: {exc}"})
        raise typer.Exit(2) from exc

    if not get_ai_client().is_available():
        _emit({"ok": False, "error": "no AI provider configured — run: navig connect"})
        raise typer.Exit(2)

    from navig_text.design import design_edit

    try:
        result = asyncio.run(design_edit(html, prompt, selector=selector, styles=styles))
    except Exception as exc:  # provider / network / quota / non-HTML — surface it as JSON
        _emit({"ok": False, "error": str(exc)})
        raise typer.Exit(1) from exc

    _emit({"ok": True, **result.to_dict()})


@design_app.command("check")
def design_check(
    as_json: bool = typer.Option(False, "--json", help="Emit the status as JSON."),
) -> None:
    """Show whether AI design edits are ready (an AI provider is configured)."""
    from navig.agent.ai_client import get_ai_client

    client = get_ai_client()
    ready = client.is_available()
    provider = getattr(client, "provider", "none")
    model = getattr(client, "model", None)

    if as_json:
        ch.emit_json({"ready": ready, "provider": provider, "model": model})
        return

    table = ch.create_table(
        columns=[
            {"name": "provider", "style": "cyan"},
            {"name": "model", "style": "magenta"},
            {"name": "status"},
            {"name": "next step"},  # free-text column
        ],
    )
    if ready:
        table.add_row(provider, model or "—", "[green]● ready[/green]", '→ navig design edit …')
    else:
        table.add_row(provider, model or "—", "[dim]○ not configured[/dim]", "→ navig connect")
    ch.print_table(table)

    if ready:
        ch.dim(f"Design edits ready via {provider} · used by the Design Mode lens's “Apply with AI”.")
    else:
        ch.warning("No AI provider configured", "Connect one:  navig connect")


@tokens_app.command("save")
def tokens_save(
    b64: Optional[str] = typer.Option(None, "--b64", help="Base64 JSON token set — used by the lens."),
    file: Optional[Path] = typer.Option(None, "--file", "-f", help="Read the token JSON from a file."),
    space: Optional[str] = typer.Option(None, "--space", help="Save into a space's design system (default: global)."),
) -> None:
    """Persist a token set as tokens.json + tokens.css (global, or a space's .navig/design)."""
    if b64:
        data = _decode_b64(b64)
    elif file:
        try:
            data = _json.loads(Path(file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _emit({"ok": False, "error": f"could not read {file}: {exc}"})
            raise typer.Exit(2) from exc
    else:
        _emit({"ok": False, "error": "provide --b64 or --file"})
        raise typer.Exit(2)

    from navig_text.design_tokens import save_tokens

    try:
        res = save_tokens(data, space=space)
    except Exception as exc:  # empty/malformed set, unknown space, or IO — surface as JSON
        _emit({"ok": False, "error": str(exc)})
        raise typer.Exit(1) from exc

    _emit({"ok": True, **res})


@tokens_app.command("show")
def tokens_show(
    space: Optional[str] = typer.Option(None, "--space", help="Show a space's design system (default: global)."),
    as_json: bool = typer.Option(False, "--json", help="Emit the token set as JSON."),
) -> None:
    """Show the stored design tokens (colors, type scale, spacing, radii)."""
    from navig_text.design_tokens import load_tokens

    try:
        tokens = load_tokens(space=space)
    except ValueError as exc:  # unknown space
        if as_json:
            _emit({"ok": False, "error": str(exc)})
        else:
            ch.error("Cannot show tokens", str(exc))
        raise typer.Exit(2) from exc

    if tokens is None:
        if as_json:
            _emit({"ok": False, "error": "no tokens saved"})
        else:
            ch.warning(
                "No design tokens saved yet",
                'Extract them: Design Mode lens → "🎨 Extract tokens" → 💾 Save',
            )
        raise typer.Exit(1)

    if as_json:
        _emit({"ok": True, "tokens": tokens})
        return

    table = ch.create_table(
        columns=[{"name": "group", "style": "cyan"}, {"name": "count"}, {"name": "values", "style": "dim"}],
    )
    for key, label in (
        ("colors", "colors"),
        ("fontSizes", "font sizes"),
        ("fontWeights", "weights"),
        ("spacing", "spacing"),
        ("radii", "radii"),
        ("fonts", "families"),
    ):
        vals = tokens.get(key, []) or []
        if vals:
            preview = ", ".join(str(v) for v in vals[:8]) + ("…" if len(vals) > 8 else "")
            table.add_row(label, str(len(vals)), preview)
    ch.print_table(table)
    ch.dim("CSS variables saved alongside as tokens.css · re-extract from the Design Mode lens to refresh.")
