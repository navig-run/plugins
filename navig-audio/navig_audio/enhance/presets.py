"""Enhancement presets (tool ``audio.enhance``) — through navig's catalog when present.

Inside navig the universal preset catalog (``navig.presets``) is the source: it
layers the operator's own edits and saved presets over the files shipped in
``navig_audio/presets/``. Standalone (``navig-audio`` without navig) the shipped
files are read directly, so the same ids work either way.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

PRESET_DIR = Path(__file__).resolve().parents[1] / "presets"
TOOL = "audio.enhance"
DEFAULT_ID = "audio-enhance/preserve-original"


def _shipped() -> list[dict[str, Any]]:
    import yaml

    out = []
    for f in sorted(PRESET_DIR.glob("*.preset.yaml")):
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("tool") == TOOL:
            out.append(data)
    return out


def list_presets() -> list[dict[str, Any]]:
    """Every ``audio.enhance`` preset as ``{id, name, description, category, params, source}``."""
    try:
        from navig.presets import catalog
    except ImportError:
        return [{**p, "source": "plugin"} for p in _shipped()]
    return [
        {"id": p.id, "name": p.name, "description": p.description, "category": p.category,
         "params": p.params, "source": p.source, "favorite": p.favorite}
        for p in catalog.list_presets(tool=TOOL)
    ]


def load(pid: str) -> dict[str, Any]:
    """The typed params of preset ``pid``. Raises KeyError if it doesn't exist."""
    try:
        from navig.presets import catalog
    except ImportError:
        for p in _shipped():
            if p.get("id") == pid:
                return dict(p.get("params") or {})
        raise KeyError(pid) from None
    p = catalog.get_preset(pid)
    if p is None or p.tool != TOOL:
        raise KeyError(pid)
    catalog.record_use([p.id])
    return dict(p.params)


def default_id() -> str:
    try:
        from navig.presets import catalog

        return catalog.get_default(TOOL) or DEFAULT_ID
    except ImportError:
        return DEFAULT_ID
