"""Turn an AI reply into JSON: strict first, forgiving second, never silently wrong.

Models wrap JSON in prose or code fences, prepend a ``<think>`` block, or run out of tokens
half-way through a long list. :func:`parse_ai_json` tries, in order: the reply as-is; the
reply without fences / think blocks; the outermost ``{…}`` or ``[…]`` slice; and finally
:func:`repair_truncated_json`, which cuts a truncated reply back to its last complete element
and closes what is still open. Whatever it returns is real JSON the model wrote — a repaired
reply only ever LOSES trailing elements, it never invents any. Callers then validate the
content (every id present, no unknown ids), so a lost element shows up as "missing" and is
retried, not as a wrong answer.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


class AIResponseError(ValueError):
    """The reply held no usable JSON."""


def repair_truncated_json(text: str) -> str:
    """Close a JSON document that was cut off mid-way. Raises ``ValueError`` if hopeless.

    Scans once, tracking strings and open brackets, and remembers the last point where the
    prefix ended on a complete element (just before a ``,`` or just after a closing bracket).
    The prefix up to there plus the matching closers is valid JSON.
    """
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise ValueError("no JSON object or array in the reply")
    s = text[min(starts):]
    stack: list[str] = []
    in_str = escaped = False
    last_safe: tuple[int, list[str]] | None = None
    for i, ch in enumerate(s):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack:
                break
            stack.pop()
            if not stack:
                return s[: i + 1]  # complete after all — just trailing junk
            last_safe = (i + 1, list(stack))
        elif ch == ",":
            last_safe = (i, list(stack))
    if last_safe is None:
        raise ValueError("the reply was cut off before its first complete element")
    idx, open_ = last_safe
    body = s[:idx].rstrip().rstrip(",")
    return body + "".join("}" if c == "{" else "]" for c in reversed(open_))


def parse_ai_json(text: str) -> Any:
    """The JSON in an AI reply. Raises :class:`AIResponseError` when there is none."""
    if not isinstance(text, str) or not text.strip():
        raise AIResponseError("the AI returned an empty reply")
    candidates = [text]
    cleaned = _THINK.sub("", text).strip()
    fence = _FENCE.search(cleaned)
    if fence:
        cleaned = fence.group(1).strip()
    candidates.append(cleaned)
    starts = [i for i in (cleaned.find("{"), cleaned.find("[")) if i >= 0]
    if starts:
        start = min(starts)
        end = max(cleaned.rfind("}"), cleaned.rfind("]"))
        if end > start:
            candidates.append(cleaned[start : end + 1])
    for cand in candidates:
        try:
            return json.loads(cand)
        except ValueError:
            continue
    try:
        return json.loads(repair_truncated_json(cleaned))
    except ValueError as exc:
        raise AIResponseError(f"the AI reply was not JSON ({exc}): {cleaned[:120]!r}") from None
