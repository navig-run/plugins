"""Episode recipes — an episode's whole cut, portable as one JSON file.

An episode is a folder of small text files that took hours to tune (scene JSON, clip
shotlists, the narration plan in ``rebuild.json``) plus real captures (``*.cast``, already
redacted at capture time) — and a pile of regenerable output (renders, mixes, voice
cache). Only the first two are irreplaceable, and before this they lived on one disk.

``export`` packs the irreplaceable part — every scene, shotlist, narration plan and the
casts the scenes play — with a sha256 per file. ``apply`` unpacks it into a folder,
verifies every hash, and refuses to overwrite a file that differs unless told to.
Binary assets (images, footage, beds) are LISTED with their hash but not carried: the
recipe stays a few hundred KB and an apply reports exactly which ones the target lacks.

A path that leaves the episode folder (a promo playing ``../e06/check.cast``) is recorded
as external and never written by ``apply`` — a recipe is data, and data does not get to
choose where on disk it lands.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

FORMAT = "navig-episode-recipe"
VERSION = 1
#: The text sources of an episode — what a re-cut needs and a render cannot recreate.
#: ``_vo/build_vo.py`` + ``_vo/beats.json`` are the narration plan of episodes cut before
#: ``rebuild.json`` existed (E07's lines live only there — the first export missed them).
SOURCE_GLOBS = ("scene*.json", "clip*.md", "rebuild*.json", "posting*.json",
                "_vo/build_vo.py", "_vo/beats.json")
#: Never carried even when a glob matches: compiled pages are regenerated from the scene.
SKIP_SUFFIXES = (".stage.html",)
_ASSET_REF = re.compile(r'(?:image|cast|art)"\s*:\s*"([^"]+)"|image="([^"]+)"|audio:\s*(\S+)')


class RecipeError(RuntimeError):
    """A recipe could not be exported, read, or applied safely."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(path: Path) -> str:
    """Exact text, line endings included — a recipe round-trips bytes, not lines."""
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def _inside(base: Path, rel: str) -> bool:
    try:
        (base / rel).resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _refs(text: str) -> set[str]:
    """Paths a scene or shotlist points at (casts, images, footage, audio)."""
    out: set[str] = set()
    for m in _ASSET_REF.finditer(text):
        ref = next(g for g in m.groups() if g).strip()
        if not ref or ref.startswith(("http:", "https:", "stage:", "data:")):
            continue
        if not Path(ref).suffix:
            continue  # `"art": "kraken"` names built-in brand art, not a file on disk
        out.add(ref)
    return out


def export(episode: Path) -> dict[str, Any]:
    """Pack ``episode``'s irreplaceable sources into a recipe dict."""
    episode = Path(episode)
    if not episode.is_dir():
        raise RecipeError(f"not an episode folder: {episode}")
    files: dict[str, dict[str, str]] = {}
    for pattern in SOURCE_GLOBS:
        for p in sorted(episode.glob(pattern)):
            if p.is_file() and not p.name.endswith(SKIP_SUFFIXES):
                text = _read(p)
                files[p.relative_to(episode).as_posix()] = {"text": text, "sha256": _sha(text.encode("utf-8"))}
    if not any(name.startswith("scene") for name in files):
        raise RecipeError(f"{episode.name} has no scene*.json — nothing to export")
    refs: set[str] = set()
    for body in files.values():
        refs |= _refs(body["text"])
    assets: dict[str, dict[str, Any]] = {}
    for ref in sorted(refs):
        p = (episode / ref)
        entry: dict[str, Any] = {"external": not _inside(episode, ref)}
        if p.is_file():
            data = p.read_bytes()
            entry["sha256"] = _sha(data)
            entry["bytes"] = len(data)
            if p.suffix == ".cast" and not entry["external"]:
                # captures are the perishable asset — carried, they are small text
                files[ref] = {"text": data.decode("utf-8"), "sha256": entry["sha256"]}
                continue
        else:
            entry["missing"] = True
        assets[ref] = entry
    return {"format": FORMAT, "version": VERSION, "episode": episode.name,
            "files": files, "assets": assets}


def load(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RecipeError(f"cannot read recipe {path}: {exc}") from exc
    if doc.get("format") != FORMAT:
        raise RecipeError(f"{path} is not a {FORMAT}")
    if int(doc.get("version", 0)) > VERSION:
        raise RecipeError(f"{path} is recipe v{doc.get('version')} — this navig reads up to v{VERSION}")
    return doc


def apply(recipe: dict[str, Any], target: Path, *, force: bool = False,
          dry_run: bool = False) -> dict[str, list[str]]:
    """Unpack ``recipe`` into ``target``. Returns what was written / kept / refused / lacking."""
    target = Path(target)
    report: dict[str, list[str]] = {"written": [], "unchanged": [], "conflict": [],
                                    "refused": [], "missing_assets": []}
    plan: list[tuple[Path, str]] = []
    for rel, body in (recipe.get("files") or {}).items():
        text = body.get("text", "")
        if _sha(text.encode("utf-8")) != body.get("sha256"):
            raise RecipeError(f"{rel}: content does not match its sha256 — the recipe is damaged")
        if Path(rel).is_absolute() or not _inside(target, rel):
            report["refused"].append(rel)
            continue
        dst = target / rel
        if dst.exists():
            if _read(dst) == text:
                report["unchanged"].append(rel)
                continue
            if not force:
                report["conflict"].append(rel)
                continue
        plan.append((dst, text))
    if report["conflict"] and not force:
        return report  # all-or-nothing: never leave half an episode updated
    for dst, text in plan:
        report["written"].append(str(dst.relative_to(target)))
        if not dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            with open(dst, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
    for ref, entry in (recipe.get("assets") or {}).items():
        base = target if not entry.get("external") else None
        if base is not None and not (base / ref).is_file():
            report["missing_assets"].append(ref)
    return report
