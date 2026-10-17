"""Where the kinetic renderer reads and writes — bound once per command.

The engine's modules kept the original script's global-style paths
(``PLATES``/``KIT``/``VOC``/``OUT``/``MARK``); this is the single place they
come from, so every path stays workspace-relative and two renders in one
process can target different workspaces. :func:`bind` is called by the command
before anything else runs; :func:`ctx` refuses to guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = ["Ctx", "bind", "ctx"]


@dataclass(frozen=True)
class Ctx:
    ws: Path
    plates: Path          # <ws>/plates/gen — stills and clips
    kit: Path             # <ws>/sound/kit — sfx one-shots
    voc: Path             # <ws>/vo/kinetic — the shared voice cache
    out: Path             # <ws>/out/kinetic — renders (overridable with --out)
    sound: Path           # <ws>/sound — ambience + grid caches
    mark: Path            # <ws>/mark.png


_CTX: Ctx | None = None


def bind(ws: Path, out: Path | None = None) -> Ctx:
    """Point the renderer at a workspace (``out`` overrides the output root)."""
    global _CTX
    ws = Path(ws)
    _CTX = Ctx(
        ws=ws,
        plates=ws / "plates" / "gen",
        kit=ws / "sound" / "kit",
        voc=ws / "vo" / "kinetic",
        out=Path(out) if out else ws / "out" / "kinetic",
        sound=ws / "sound",
        mark=ws / "mark.png",
    )
    return _CTX


def ctx() -> Ctx:
    if _CTX is None:
        raise RuntimeError("kinetic renderer used before bind() — a command bug")
    return _CTX
