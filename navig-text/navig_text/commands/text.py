"""``navig text`` — AI text generation (drafts · articles · captions · briefs).

The TEXT facet's user-facing verb. Inside navig, generation goes through navig's AI
(multi-provider fallback); on its own (`navig-text`) through the user's own AI — any
provider key, an OpenAI-compatible URL, or a local Ollama:

  navig text gen "explain vector databases in 5 bullets"           # a draft
  navig text gen "NAVIG weekly update" --kind article --out ./out  # a full article
  navig text gen "ship faster with NAVIG" --kind caption           # one caption
  navig text gen "NAVIG v2 launch" --kind brief                    # a fan-out brief
  navig text check                                                 # is a provider ready?
  navig text lyrics fit draft.md --bpm 88                          # does a lyric sit on the beat?

The generated text is printed and saved as Markdown; nothing here prints a secret.
"""

from __future__ import annotations

import asyncio
import json as _json
from pathlib import Path
from typing import Optional

import typer

from navig_sdk import ai
from navig_sdk import console as ch
from navig_sdk.host import config_dir, navig_available

from navig_sdk.host import command_name  # noqa: E402

# The command a user types here: `navig text` inside navig, `navig-text` on its own.
CMD = command_name("text", standalone="navig-text")


def _setup_hint() -> str:
    """How to get an AI configured HERE (navig's command inside navig, env vars on its own)."""
    if navig_available():
        return "Connect one:  navig connect  (or set a provider key, e.g. OPENAI_API_KEY)"
    return ("Set a provider key (e.g. OPENAI_API_KEY or OPENROUTER_API_KEY), point "
            "NAVIG_AI_BASE_URL + NAVIG_AI_MODEL at any OpenAI-compatible server, or run Ollama")

text_app = typer.Typer(
    name="text",
    help="📝 AI text generation — drafts, articles, captions, fan-out briefs — and lyrics on a beat.",
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

    if not ai.status()["ready"]:
        ch.error("No AI provider configured", _setup_hint())
        raise typer.Exit(2)

    from navig_text.generation import generate_text_docs

    out_dir = str(out) if out is not None else str(config_dir() / "text")

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
    st = ai.status()
    ready, provider, model = st["ready"], st["provider"] or "none", st["model"]

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
        table.add_row(provider, model or "—", "[green]● ready[/green]", f"→ {CMD} gen \"…\"")
    else:
        table.add_row(provider, model or "—", "[dim]○ not configured[/dim]",
                      "→ navig connect" if navig_available() else "→ set a provider key")
    ch.print_table(table)

    if ready:
        ch.dim(f"Text generation ready via {provider} · {CMD} gen \"<prompt>\" --kind draft|article|caption|brief")
    else:
        ch.warning("No AI provider configured", _setup_hint())


# `navig text lyrics …` — lyrics on a beat (no AI: fit, scaffold, word bank, demo sheets).
from navig_text.commands.lyrics import lyrics_app  # noqa: E402

text_app.add_typer(lyrics_app, name="lyrics")
