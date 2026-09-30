"""navig-contacts plugin entry point.

Deliberately registers **no** ModuleDef. Core already carries a `contacts`
module definition, and it knows things this package does not: that Contacts
renders as a tab inside the desktop Messages app (`merged_into="messages"`,
which is what keeps its deep-links alive) and that it requires the gateway.
Registering a second definition for the same id replaced that one and quietly
promoted Contacts to a top-level tile.

The CLI is the whole of this package's surface, and it registers through the
``navig.commands`` entry-point group in pyproject.toml.
"""

from __future__ import annotations


def register() -> None:  # pragma: no cover - nothing to register
    """No-op: core owns the `contacts` module definition."""
    return None
