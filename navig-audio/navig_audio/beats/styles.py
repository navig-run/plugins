"""Beat styles — the named presets ``navig audio beat gen`` renders from.

A style is a producer's brief written down once: tempo, key, what the drums and bass
do, what each section adds, and what the model must NOT reach for. The built-in file
next to this module holds generic genres; a project keeps its own in a YAML the command
takes with ``--styles`` (a persona's house sound is not navig's business to ship).

Two files can define the same id — the later one wins outright — and a style may
``extends`` another, so a house preset is "boom-bap-dark plus our toys" rather than a
copy that drifts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BUILTIN_STYLES = Path(__file__).with_name("styles.yaml")

SECTION_NAMES = ("intro", "verse", "hook", "bridge", "breakdown", "outro")
DEFAULT_STRUCTURE = "intro:4,verse:16,hook:8,verse:16,hook:8,breakdown:4,outro:4"

# Appended to every render: the whole point of a beat is that the voice is added later.
INSTRUMENTAL_NEGATIVE = ("vocals", "singing", "rap vocals", "lyrics", "spoken word", "choir")


class StyleError(ValueError):
    """A style file or id the command cannot use."""


@dataclass
class BeatStyle:
    id: str
    label: str = ""
    family: str = ""
    language: str = ""
    bpm: float = 90.0
    bpm_range: tuple[float, float] = (60.0, 200.0)
    key: str = "A minor"
    positive: list[str] = field(default_factory=list)
    negative: list[str] = field(default_factory=list)
    sections: dict[str, list[str]] = field(default_factory=dict)
    structure: str = DEFAULT_STRUCTURE
    notes: str = ""
    source: str = "builtin"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "family": self.family,
            "language": self.language,
            "bpm": self.bpm,
            "bpm_range": list(self.bpm_range),
            "key": self.key,
            "positive": list(self.positive),
            "negative": list(self.negative),
            "sections": {k: list(v) for k, v in self.sections.items()},
            "structure": self.structure,
            "notes": self.notes,
            "source": self.source,
        }


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - pyyaml ships with navig
        raise StyleError("reading a styles file needs pyyaml") from exc
    if not path.exists():
        raise StyleError(f"styles file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise StyleError(f"{path.name}: expected a mapping at the top level")
    styles = data.get("styles", data)
    if not isinstance(styles, dict):
        raise StyleError(f"{path.name}: 'styles' must be a mapping of id → style")
    return styles


def _build(sid: str, raw: dict[str, Any], base: BeatStyle | None, source: str) -> BeatStyle:
    style = BeatStyle(id=sid, source=source)
    if base is not None:
        style.label, style.family, style.language = base.label, base.family, base.language
        style.bpm, style.bpm_range, style.key = base.bpm, base.bpm_range, base.key
        style.positive, style.negative = list(base.positive), list(base.negative)
        style.sections = {k: list(v) for k, v in base.sections.items()}
        style.structure, style.notes = base.structure, base.notes
    if "label" in raw:
        style.label = str(raw["label"])
    if "family" in raw:
        style.family = str(raw["family"])
    if "language" in raw:
        style.language = str(raw["language"])
    if "bpm" in raw:
        style.bpm = float(raw["bpm"])
    if "bpm_range" in raw:
        lo, hi = raw["bpm_range"]
        style.bpm_range = (float(lo), float(hi))
    if "key" in raw:
        style.key = str(raw["key"])
    if "positive" in raw:
        style.positive = _as_list(raw["positive"])
    if "positive_add" in raw:
        style.positive += _as_list(raw["positive_add"])
    if "negative" in raw:
        style.negative = _as_list(raw["negative"])
    if "negative_add" in raw:
        style.negative += _as_list(raw["negative_add"])
    if "sections" in raw and isinstance(raw["sections"], dict):
        for name, styles in raw["sections"].items():
            style.sections[str(name)] = _as_list(styles)
    if "sections_add" in raw and isinstance(raw["sections_add"], dict):
        for name, styles in raw["sections_add"].items():
            style.sections.setdefault(str(name), [])
            style.sections[str(name)] += _as_list(styles)
    if "structure" in raw:
        style.structure = str(raw["structure"])
    if "notes" in raw:
        style.notes = str(raw["notes"])
    if not style.label:
        style.label = sid
    return style


def load_styles(extra_files: list[Path] | None = None, *, builtin: Path | None = BUILTIN_STYLES) -> dict[str, BeatStyle]:
    """Built-in styles, then each extra file in order; later definitions win by id."""
    styles: dict[str, BeatStyle] = {}
    files: list[tuple[Path, str]] = []
    if builtin is not None:
        files.append((builtin, "builtin"))
    for f in extra_files or []:
        files.append((Path(f), Path(f).name))
    for path, source in files:
        raw_styles = _read_yaml(path)
        # Resolve `extends` within the file in dependency order: a parent defined later in
        # the same file is still a parent.
        pending = dict(raw_styles)
        progress = True
        while pending and progress:
            progress = False
            for sid in list(pending):
                raw = pending[sid]
                if not isinstance(raw, dict):
                    raise StyleError(f"{path.name}: style {sid!r} must be a mapping")
                parent = raw.get("extends")
                if parent and parent not in styles:
                    if parent in pending:
                        continue  # wait for it
                    raise StyleError(f"{path.name}: style {sid!r} extends unknown {parent!r}")
                styles[sid] = _build(sid, raw, styles.get(parent) if parent else None, source)
                del pending[sid]
                progress = True
        if pending:
            raise StyleError(f"{path.name}: circular 'extends' among {sorted(pending)}")
    return styles


def get_style(styles: dict[str, BeatStyle], sid: str) -> BeatStyle:
    try:
        return styles[sid]
    except KeyError:
        known = ", ".join(sorted(styles)) or "none"
        raise StyleError(f"unknown style {sid!r} — known: {known}") from None
