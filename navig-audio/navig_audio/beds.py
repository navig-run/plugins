"""Music that fits the episode and never repeats across the channel.

Five beds served every episode, so a viewer heard the same track three posts
in a row. Here each episode declares a mood; a track is drawn from that mood's
pool (royalty-free folders only, long mixes excluded) and ASSIGNED to the
episode for good — no two episodes share a track. The assignment is stored, so
a re-render keeps its music.

Ported from the channel workspace's ``music.py``. The store lives with the
workspace (``<ws>/sound/bed-assign.json``) — the assignment is channel data,
not plugin state — and the library root comes from ``<ws>/beds.json`` when the
media sits somewhere other than the default drive.

Pools are built from filenames in the cleared library:
  dark     Dark & Horror, darksynth, industrial, apocalyptic
  mystery  atmospheric / ambient / sci-fi / night
  humor    chill & retro synthwave — light, bouncy, deadpan-friendly
  epic     cinematic & epic, trailer themes
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

__all__ = ["EPISODE_MOOD", "MOODS", "BedError", "assign", "library_root", "mood_for", "pool", "store_path"]

DEFAULT_ROOT = Path(r"X:\Assets\Audio\Royalty-Free")

#: Per-episode mood when the episode declares none — the channel's own pairing
#: (was ``MOOD`` in the workspace's kinetic.py; it belongs with the pools it
#: selects from, so the ``bed assign`` verb and the renderer cannot disagree).
EPISODE_MOOD = {
    "genesis": "epic", "soul": "mystery", "neighbour": "dark", "campaign": "humor",
    "firstrule": "dark", "yesbut": "humor", "architect": "mystery", "apocalypse": "humor",
    "ghost": "mystery", "loading": "humor", "pigeongate": "humor", "scroll": "dark",
    "halloween": "mystery", "pigeon": "humor",
}

SKIP = re.compile(r"mix|hour|\bhr\b|mixtape|playlist|alan walker|websites|cheapest|"
                  r"4k|jazz|house music|death metal|slideshow|copyright free music_|#shorts|best chill music", re.I)
MOODS = {
    "dark": (["Dark & Horror", "Synthwave & Retro", "General"],
             re.compile(r"horror|darksynth|industrial|apocalyptic|slasher|doom|evil|darkness", re.I)),
    "mystery": (["Synthwave & Retro", "General", "Dark & Horror"],
                re.compile(r"atmospheric|ambient|sci-fi|night|distant|nexus|traveler|void|"
                           r"frozen|titan|fermi|alone|sanity|hyperion", re.I)),
    "humor": (["Synthwave & Retro", "Ambient & Chill"],
              re.compile(r"chill|retro synthwave|sunset|arcadia|neon|countach|impala|"
                         r"sakura|vhs memory|montage|feel the heat|vaporwave", re.I)),
    "epic": (["Cinematic & Epic", "Synthwave & Retro", "General"],
             re.compile(r"cinematic|epic|trailer|legend|anti hero|art of war|undercover|chase", re.I)),
}


class BedError(RuntimeError):
    """No track could be assigned; the message says why."""


def library_root(workspace: Path) -> Path:
    """Where the cleared library lives: ``<ws>/beds.json`` → default drive."""
    cfg = Path(workspace) / "beds.json"
    if cfg.is_file():
        try:
            root = json.loads(cfg.read_text(encoding="utf-8")).get("root")
            if root:
                return Path(root)
        except (OSError, ValueError):
            pass
    return DEFAULT_ROOT


def store_path(workspace: Path) -> Path:
    """The persistent assignment store — channel data, lives with the channel."""
    return Path(workspace) / "sound" / "bed-assign.json"


def mood_for(name: str, episode: dict) -> str:
    """The mood to draw a pool from: the episode's own, else the table, else dark."""
    return str(episode.get("mood") or EPISODE_MOOD.get(name, "dark"))


def _dur(p: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(p)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def pool(mood: str, workspace: Path) -> list[Path]:
    if mood not in MOODS:
        raise BedError(f"unknown mood {mood!r} — one of {', '.join(MOODS)}")
    folders, rx = MOODS[mood]
    root = library_root(workspace)
    out = []
    for f in folders:
        for p in sorted((root / f).glob("*.mp3")):
            if rx.search(p.name) and not SKIP.search(p.name):
                out.append(p)
    return out


def assign(name: str, mood: str, workspace: Path) -> tuple[Path, float]:
    """(track, start offset) for an episode — stable, and unique per episode."""
    store = store_path(workspace)
    data = json.loads(store.read_text(encoding="utf-8")) if store.exists() else {}
    if name in data and Path(data[name]["path"]).exists():
        return Path(data[name]["path"]), data[name]["at"]
    taken = {v["path"] for v in data.values()}
    cands = sorted(pool(mood, workspace),
                   key=lambda p: hashlib.sha1(f"{name}|{p.name}".encode()).hexdigest())
    for p in cands:
        if str(p) in taken:
            continue
        d = _dur(p)
        if 40 <= d <= 600:                      # a track, not a 2-hour mix
            at = round(min(25.0, d * 0.12), 1)  # past the intro, into the groove
            data[name] = {"path": str(p), "at": at, "mood": mood}
            store.parent.mkdir(parents=True, exist_ok=True)
            store.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
            return p, at
    raise BedError(
        f"no free track left in the '{mood}' pool for {name} — free a taken "
        "track in the store, or point beds.json at a bigger library"
    )
