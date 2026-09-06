"""Canonical brand-cased display names for social networks/providers.

ONE source of truth for turning a network id (``"linkedin"``) into the name a
human sees (``"LinkedIn"``). Consumed by the publisher registry (→ the
``/studio/networks`` API that feeds every webui surface) and the ``navig social``
CLI tables (``status`` / ``sync`` / ``report``).

Deliberately a **leaf module** — it imports nothing from navig or this package —
so any layer can import it with zero circular-import risk. Only list ids that a
plain ``.title()`` gets *wrong*; everything else derives correctly from the id.
"""

from __future__ import annotations

# id → brand casing. ONLY the ids ``.title()`` mangles need an entry here.
DISPLAY_LABELS: dict[str, str] = {
    "linkedin": "LinkedIn",
    "youtube": "YouTube",
    "devto": "Dev.to",
}


def display_label(name: str) -> str:
    """Brand-cased display name for a network/provider id.

    Falls back to ``name.replace("_", " ").title()`` for anything not in
    :data:`DISPLAY_LABELS` (``"reddit"`` → ``"Reddit"``). A falsy ``name`` is
    returned unchanged, so callers can keep their own empty / ``"(unattributed)"``
    handling (``display_label("") == ""``).
    """
    if not name:
        return name
    return DISPLAY_LABELS.get(name.lower(), name.replace("_", " ").title())
