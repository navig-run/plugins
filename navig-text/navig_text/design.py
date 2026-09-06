"""AI "design edit" — restyle/rewrite ONE HTML element from an instruction.

The agent backend behind the Design Mode browser lens's "Apply with AI": the
lens sends one element's outer HTML + a natural-language instruction, the model
returns the revised element HTML, and the lens applies it live (after its own
sanitization). Lives in navig-text because it is an agent-backed *text/markup
transform* that reuses the very same ``get_ai_client()`` primitive as text
generation — the architectural law that all inference lives in ``navig/agent/``.

Returns HTML only. The system prompt forbids scripts / event handlers / iframes,
and the caller (the lens) sanitizes again before touching the DOM — defense in
depth, since the output is inserted into a live page.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger(__name__)

_SYSTEM = (
    "You are a meticulous web design assistant. You receive ONE HTML element and "
    "an instruction describing how to restyle or rewrite it. Return ONLY the "
    "revised HTML for that single element — keep the same outer tag and its role, "
    "valid and self-contained. You MAY change inline styles, classes, text, and "
    "inner markup to satisfy the instruction. Do NOT add <script>, event-handler "
    "attributes (onclick, onload, ...), <iframe>, or external resource loads. Do "
    "NOT wrap the output in Markdown code fences. Return only the HTML, with no "
    "explanation."
)

# A leading ```lang fence and/or a trailing ``` fence, if the model adds them.
_LEAD_FENCE = re.compile(r"^```[a-zA-Z0-9]*\n?")
_TRAIL_FENCE = re.compile(r"\n?```$")


@dataclass
class DesignEdit:
    """A revised element returned by :func:`design_edit`."""

    html: str
    model: str | None = None
    instruction: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"html": self.html, "model": self.model, "instruction": self.instruction}


def _clean(raw: str) -> str:
    """Strip Markdown code fences and stray whitespace from a completion."""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = _LEAD_FENCE.sub("", s)
        s = _TRAIL_FENCE.sub("", s)
    return s.strip()


async def design_edit(
    html: str,
    instruction: str,
    *,
    selector: str | None = None,
    styles: str | None = None,
) -> DesignEdit:
    """Return a revised version of one element's HTML per *instruction*.

    Raises ``ValueError`` for missing input and ``RuntimeError`` for a failed,
    empty, or non-HTML completion — never returns junk the lens would inject into
    the DOM.
    """
    if not (html or "").strip():
        raise ValueError("no element HTML provided")
    if not (instruction or "").strip():
        raise ValueError("no instruction provided")

    from navig.agent.ai_client import get_ai_client

    client = get_ai_client()

    parts = [f"Instruction: {instruction.strip()}", ""]
    if selector:
        parts.append(f"Selector: {selector}")
    if styles:
        parts.append(f"Current styles (context): {styles}")
    parts += ["", "Element HTML:", html.strip()]
    prompt = "\n".join(parts)

    raw = await client.complete(prompt, system_prompt=_SYSTEM)
    out = _clean(raw)
    # An empty completion is a provider failure (soft refusal / quota / swallowed
    # error). A non-``<`` completion means the model returned prose, not markup.
    if not out:
        raise RuntimeError("design edit returned an empty completion")
    if not out.startswith("<"):
        raise RuntimeError("design edit did not return HTML (got prose)")
    return DesignEdit(
        html=out,
        model=getattr(client, "model", None),
        instruction=instruction.strip(),
    )
