"""Shared media contract — the vocabulary every media facet (image / video / audio /
text) and the core generation engine agree on.

Phase 3.0 of the media modularization: instead of four plugins each cloning the
generation orchestrator, core keeps ONE engine and exposes a typed contract + a
**generator registry**. Each facet plugin registers its per-modality backend via
:func:`register_generator`; the engine dispatches through :func:`get_generator`
(falling back to the built-in backends when no facet is registered).

This replaces the previously bare-string modality vocabulary
(``("image", "video", "audio")``) so a typo is an error, not a silent wrong path.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MediaModality(str, Enum):
    """The kinds of media a facet can generate. ``str`` mixin → JSON/DB friendly."""

    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    TEXT = "text"

    @classmethod
    def coerce(cls, value: "MediaModality | str") -> "MediaModality":
        """Normalize a string/enum to a MediaModality, raising on an unknown value."""
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).lower())
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            raise ValueError(f"unknown media modality {value!r}; expected one of: {valid}") from exc


@dataclass
class MediaAsset:
    """A generated media asset — the shared row shape across facets + the engine.

    Mirrors the ``generated_media`` store row so plugins never hand-roll dicts for
    the same data. ``status``: ``generated`` (staged) | ``kept`` | ``rejected``.
    """

    id: str
    modality: MediaModality
    status: str = "generated"
    group_id: str | None = None
    prompt: str = ""
    provider: str | None = None
    model: str | None = None
    seed: int | None = None
    params: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    path: str | None = None
    license: str | None = None
    space: str | None = None


# ── generator backend registry ──────────────────────────────────────────────
# A backend runs a provider for one modality, writing files into ``out_dir`` and
# returning the provider's result objects (same contract as the engine's built-in
# per-modality blocks). Facet plugins call register_generator() at import/boot.
GeneratorBackend = Callable[..., Awaitable[list[Any]]]

_GENERATORS: dict[MediaModality, GeneratorBackend] = {}
_OWNERS: dict[MediaModality, str] = {}

_log = logging.getLogger("navig.media.types")


def _owner_of(backend: GeneratorBackend) -> str:
    """The distribution-level owner of *backend*: its top-level package (``navig_audio``)."""
    module = getattr(backend, "__module__", None) or ""
    return module.split(".", 1)[0] or "unknown"


def register_generator(
    modality: MediaModality | str,
    backend: GeneratorBackend,
    *,
    owner: str | None = None,
    replace: bool = False,
) -> bool:
    """Register the generation backend for *modality*. One owner per modality.

    The owner (default: the backend's top-level package) is recorded. A second
    registration by the SAME owner replaces its backend (idempotent re-import); a
    registration by a DIFFERENT owner is refused with a warning naming both, so which
    plugin generates a modality never depends on import order. ``replace=True`` is the
    explicit override (tests, a deliberate swap).

    Returns True when *backend* is now the registered one.
    """
    key = MediaModality.coerce(modality)
    who = owner or _owner_of(backend)
    current = _OWNERS.get(key)
    if current is not None and current != who and not replace:
        _log.warning(
            "media: %s generator already owned by %s; ignoring registration from %s",
            key.value, current, who,
        )
        return False
    _GENERATORS[key] = backend
    _OWNERS[key] = who
    return True


def unregister_generator(modality: MediaModality | str) -> None:
    """Drop the backend for *modality* (the engine falls back to its built-in one)."""
    key = MediaModality.coerce(modality)
    _GENERATORS.pop(key, None)
    _OWNERS.pop(key, None)


def registered_generators() -> dict[str, str]:
    """``{modality: owner}`` for every facet-registered backend (doctor / status views)."""
    return {m.value: o for m, o in _OWNERS.items()}


def get_generator(modality: MediaModality | str) -> GeneratorBackend | None:
    """The registered backend for *modality*, or None (engine falls back to built-in)."""
    return _GENERATORS.get(MediaModality.coerce(modality))


def registered_modalities() -> list[MediaModality]:
    """Modalities that currently have a facet-registered backend."""
    return list(_GENERATORS)
