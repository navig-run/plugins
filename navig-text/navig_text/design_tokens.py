"""Persist a design-token set extracted by the Design Mode lens.

The "design system" facet: the lens scans a page for its colors, type scale,
spacing, and radii, then sends the token set here to be persisted as both
``tokens.json`` (the source of truth) and ``tokens.css`` (ready-to-use CSS custom
properties) under ``data_dir()/design`` (global) or a space's ``.navig/design``
(with ``--space``) — so a captured look becomes a reusable, per-project design
system. Global storage honors ``NAVIG_DATA_DIR`` (test-isolatable).

No LLM, no network — pure aggregation + file IO. Values are sanitized before they
reach ``tokens.css`` (they originate from a browser, but ``--b64`` is arbitrary).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# Strip anything that could break out of a CSS declaration when writing the file.
_UNSAFE_CSS = re.compile(r"[;{}\n\r]")

_LIST_KEYS = ("colors", "fonts", "fontSizes", "fontWeights", "spacing", "radii", "shadows")


def _resolve_space_root(space: str) -> Path:
    """Resolve a space NAME to its root dir. Raises ``ValueError`` if unknown."""
    from navig.spaces.contracts import normalize_space_name
    from navig.spaces.resolver import discover_space_paths

    cfg = (discover_space_paths() or {}).get(normalize_space_name(space))
    root = str(getattr(cfg, "path", "") or "") if cfg is not None else ""
    if not root:
        raise ValueError(f"space not found: {space!r} (see `navig space list`)")
    return Path(root)


def _design_dir(space: str | None = None) -> Path:
    """The design-system dir: a space's ``.navig/design`` when *space* is given,
    else the global ``data_dir()/design`` (honors ``NAVIG_DATA_DIR``). No mkdir —
    callers that write create it, so ``show`` never fabricates an empty dir."""
    if space:
        return _resolve_space_root(space) / ".navig" / "design"
    from navig.platform.paths import data_dir

    return data_dir() / "design"


def _safe(v: object) -> str:
    return _UNSAFE_CSS.sub("", str(v)).strip()


def tokens_to_css(tokens: dict) -> str:
    """Render a token set as ``:root { --… }`` CSS custom properties."""
    lines = [":root {"]
    for i, c in enumerate(tokens.get("colors", []) or [], 1):
        lines.append(f"  --color-{i}: {_safe(c)};")
    for i, s in enumerate(tokens.get("fontSizes", []) or [], 1):
        lines.append(f"  --font-size-{i}: {_safe(s)}px;")
    for i, w in enumerate(tokens.get("fontWeights", []) or [], 1):
        lines.append(f"  --font-weight-{i}: {_safe(w)};")
    for i, s in enumerate(tokens.get("spacing", []) or [], 1):
        lines.append(f"  --space-{i}: {_safe(s)}px;")
    for i, r in enumerate(tokens.get("radii", []) or [], 1):
        lines.append(f"  --radius-{i}: {_safe(r)}px;")
    fonts = tokens.get("fonts", []) or []
    if fonts:
        lines.append(f"  --font-family: {_safe(fonts[0])};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _count(tokens: dict) -> int:
    return sum(
        len(tokens.get(k, []) or [])
        for k in ("colors", "fontSizes", "fontWeights", "spacing", "radii")
    )


def save_tokens(tokens: dict, space: str | None = None) -> dict[str, Any]:
    """Write ``tokens.json`` + ``tokens.css`` (global, or into *space*'s
    ``.navig/design``); return paths + a token count.

    Raises ``ValueError`` if *tokens* carries none of the known token lists (so a
    malformed/empty payload never silently overwrites a real design system), or if
    *space* is given but unknown.
    """
    if not isinstance(tokens, dict) or not any(tokens.get(k) for k in _LIST_KEYS):
        raise ValueError("token set is empty or malformed (no colors/type/spacing/…)")

    from navig.core.yaml_io import atomic_write_text

    d = _design_dir(space)
    d.mkdir(parents=True, exist_ok=True)
    json_path = d / "tokens.json"
    css_path = d / "tokens.css"
    # Atomic (temp-file + fsync + os.replace, with Windows AV/backup-lock retry):
    # a crash or transient lock mid-write can't truncate a previously-good design
    # system into an empty file that ``load_tokens`` would then read as "none saved"
    # — nor leave tokens.json (the source of truth) disagreeing with tokens.css.
    atomic_write_text(json_path, json.dumps(tokens, indent=2, ensure_ascii=False))
    atomic_write_text(css_path, tokens_to_css(tokens))
    return {"path": str(json_path), "css_path": str(css_path), "count": _count(tokens), "space": space}


def load_tokens(space: str | None = None) -> dict | None:
    """Return the stored token set (global, or *space*'s), or ``None`` if none saved.

    Raises ``ValueError`` if *space* is given but unknown, **or** if the token file
    exists but is unreadable / corrupt. A present-but-unreadable file is NOT the same
    as "none saved": masking it as ``None`` would tell the user to re-extract over a
    design system that is actually still on disk (the phantom-empty failure class) —
    surface it honestly instead."""
    p = _design_dir(space) / "tokens.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError(f"design tokens file is unreadable or corrupt: {p} ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError(f"design tokens file is not a token object: {p}")
    return data
