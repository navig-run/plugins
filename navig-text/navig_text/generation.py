"""AI text generation — the **TEXT** facet of the shared media engine.

The second facet (after audio) proving the ``register_generator`` pattern:
navig-text plugs a TEXT backend into core's generation registry
(``navig.media.types``), so ``navig generate gen --modality text`` and the deck
``/api/deck/media`` route dispatch through this plugin when installed.

The LLM call goes through core's ``navig.agent.ai_client`` — the architectural
law that *all* inference lives in ``navig/agent/`` — so the facet needs **no
external deps**. Generated documents are written as Markdown and land as
``MediaModality.TEXT`` rows in the same refs library as images / video / audio,
so a drafted caption or brief flows straight into the fan-out and the content
assembly line.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)
_REGISTERED = False

# Per-kind system prompts. ``kind`` arrives from the engine (its default is the
# audio-centric ``"music"``) → any unknown kind falls back to ``"draft"``.
_SYSTEMS: dict[str, str] = {
    "draft": (
        "You are a precise writing assistant. Produce clean, well-structured "
        "Markdown. No preamble, no meta-commentary — return only the document."
    ),
    "article": (
        "You are a senior technical writer. Write a complete, publishable Markdown "
        "article: a strong title (# heading), a tight intro, useful sections, and a "
        "short takeaway. Return only the article."
    ),
    "caption": (
        "You are a social copywriter. Write ONE short, punchy caption — no hashtags "
        "unless asked, no surrounding quotes. Return only the caption."
    ),
    "brief": (
        "You are a content strategist. Return a concise brief in Markdown: a bold "
        "**Title:** line, then a 2-3 sentence body. No preamble."
    ),
    "social": (
        "You are a social copywriter. Write ONE concise post (<= 280 characters) "
        "with a clear hook. Return only the post text."
    ),
}

KINDS = tuple(_SYSTEMS)


@dataclass
class GeneratedText:
    """A generated text document.

    Matches the generator-return contract the engine reads (``local_path`` +
    ``model`` + ``seed``); ``text`` carries the content for the CLI/pipeline.
    """

    local_path: str | None = None
    model: str | None = None
    kind: str = "draft"
    seed: int | None = None
    text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "local_path": self.local_path,
            "model": self.model,
            "kind": self.kind,
            "chars": len(self.text),
            "text": self.text,
        }


async def generate_text_docs(
    prompt: str,
    *,
    kind: str = "draft",
    n: int = 1,
    out_dir: str | Path,
    system: str | None = None,
) -> list[GeneratedText]:
    """Generate ``n`` Markdown document(s) into ``out_dir``; return the results.

    The reusable primitive behind both the ``navig text gen`` verb and the engine
    facet. Uses core's AI client (multi-provider fallback lives there).
    """
    from navig.agent.ai_client import get_ai_client

    client = get_ai_client()
    sys_prompt = system or _SYSTEMS.get(kind, _SYSTEMS["draft"])
    model = getattr(client, "model", None)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    results: list[GeneratedText] = []
    for i in range(max(1, n)):
        text = await client.complete(prompt, system_prompt=sys_prompt)
        # A None/empty completion is a provider failure (soft refusal / quota / a
        # swallowed error yielding ""). Surface it — never write an empty doc that a
        # caller (e.g. the pipeline) would then publish as a placeholder.
        if not text or not text.strip():
            raise RuntimeError("text generation returned an empty completion")
        path = out / (f"text_{i:02d}.md" if n > 1 else "text.md")
        path.write_text(text, encoding="utf-8")
        results.append(GeneratedText(local_path=str(path), model=model, kind=kind, text=text))
    return results


async def _text_backend(
    enriched_prompt: str,
    *,
    provider: str | None = None,
    size: str | None = None,
    kind: str = "draft",
    duration_s: float | None = None,
    n: int = 1,
    seed: int | None = None,
    out_dir: Path,
    **_ignored: Any,
) -> list[GeneratedText]:
    """``GeneratorBackend`` for ``MediaModality.TEXT`` (see ``navig.media.types``)."""
    resolved_kind = kind if kind in _SYSTEMS else "draft"
    return await generate_text_docs(enriched_prompt, kind=resolved_kind, n=n, out_dir=out_dir)


def register_text_facet() -> None:
    """Register the TEXT backend into core's generation registry (idempotent).

    No-op when core is too old to expose the registry (engine then has no text
    backend, which is fine — text is a new modality).
    """
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        from navig.media.types import MediaModality, register_generator

        register_generator(MediaModality.TEXT, _text_backend)
        _REGISTERED = True
        _log.debug("navig-text: registered TEXT generation facet")
    except Exception as exc:  # pragma: no cover - defensive (old core / import order)
        _log.debug("navig-text: text facet not registered (%s)", exc)
