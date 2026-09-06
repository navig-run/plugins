"""``navig text`` — AI text generation (drafts · articles · captions · briefs).

The TEXT facet's user-facing verb. Generation goes through core's AI client
(multi-provider fallback), so no key is needed beyond whatever provider you've
already configured:

  navig text gen "explain vector databases in 5 bullets"           # a draft
  navig text gen "NAVIG weekly update" --kind article --out ./out  # a full article
  navig text gen "ship faster with NAVIG" --kind caption           # one caption
  navig text gen "NAVIG v2 launch" --kind brief                    # a fan-out brief
  navig text check                                                 # is a provider ready?

The generated text is printed and saved as Markdown; nothing here prints a secret.
"""

from __future__ import annotations

import asyncio
import json as _json
from pathlib import Path
from typing import Optional

import typer

from navig.lazy_loader import lazy_import

ch = lazy_import("navig.console_helper")

text_app = typer.Typer(
    name="text",
    help="📝 AI text generation — drafts, articles, captions, and fan-out briefs.",
    no_args_is_help=True,
)


def _kinds() -> tuple[str, ...]:
    from navig_text.generation import KINDS

    return KINDS


@text_app.command("gen")
def text_gen(
    prompt: str = typer.Argument(..., help="What to write (a topic, instruction, or brief)."),
    kind: str = typer.Option("draft", "--kind", "-k", help="draft | article | caption | brief | social"),
    system: Optional[str] = typer.Option(
        None, "--system", "-s", help="Override the system prompt for full control."
    ),
    count: int = typer.Option(1, "--count", "-n", min=1, max=10, help="How many variants to generate."),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="Directory to save the .md into (default: ~/.navig/text)."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit the result(s) as JSON."),
) -> None:
    """Generate Markdown text from a prompt and save it locally."""
    kind_l = kind.lower().strip()
    if kind_l not in _kinds():
        ch.error(f"Unknown kind {kind!r}", f"Use one of: {', '.join(_kinds())}")
        raise typer.Exit(2)

    try:
        from navig.agent.ai_client import get_ai_client
    except ImportError as exc:  # pragma: no cover
        ch.error("Text generation unavailable", str(exc))
        raise typer.Exit(2) from exc

    if not get_ai_client().is_available():
        ch.error(
            "No AI provider configured",
            "Connect one:  navig connect  (or set a provider key, e.g. OPENAI_API_KEY)",
        )
        raise typer.Exit(2)

    from navig_text.generation import generate_text_docs

    out_dir = str(out) if out is not None else str(Path("~/.navig/text").expanduser())

    with ch.create_spinner(f"Writing {kind_l}…"):
        try:
            results = asyncio.run(
                generate_text_docs(prompt, kind=kind_l, n=count, out_dir=out_dir, system=system)
            )
        except Exception as exc:  # provider / network / quota — surface it
            ch.error("Text generation failed", str(exc))
            raise typer.Exit(1) from exc

    if as_json:
        ch.raw_print(_json.dumps([r.to_dict() for r in results], indent=2, ensure_ascii=False))
        return

    for idx, r in enumerate(results, 1):
        if len(results) > 1:
            ch.subheader(f"Variant {idx}/{len(results)}  ·  {r.kind}")
        ch.raw_print(r.text.rstrip())
        ch.dim(f"↳ saved {r.local_path}")
    ch.success(f"Generated {len(results)} {kind_l} document(s)", f"Saved under {out_dir}")


@text_app.command("check")
def text_check(
    as_json: bool = typer.Option(False, "--json", help="Emit the status as JSON."),
) -> None:
    """Show whether AI text generation is ready (a provider is configured)."""
    from navig.agent.ai_client import get_ai_client

    client = get_ai_client()
    ready = client.is_available()
    provider = getattr(client, "provider", "none")
    model = getattr(client, "model", None)

    if as_json:
        ch.raw_print(
            _json.dumps({"ready": ready, "provider": provider, "model": model}, indent=2)
        )
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
        table.add_row(provider, model or "—", "[green]● ready[/green]", "→ navig text gen \"…\"")
    else:
        table.add_row(provider, model or "—", "[dim]○ not configured[/dim]", "→ navig connect")
    ch.print_table(table)

    if ready:
        ch.dim(f"Text generation ready via {provider} · navig text gen \"<prompt>\" --kind draft|article|caption|brief")
    else:
        ch.warning("No AI provider configured", "Connect one:  navig connect")
