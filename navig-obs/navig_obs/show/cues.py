"""The cue book (broadcast contract-04 ``cues.json``) + per-cue cooldowns.

Only the fields playback needs are read; the contract's required fields are
still checked so a cue without provenance never plays (the media vault holds no
license records — provenance is the only record).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

_CUE_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_REQUIRED = ("cueId", "file", "trigger", "lufsTarget", "provenance")


class CueBookError(ValueError):
    """The cue book is missing, unreadable, or breaks the contract."""


@dataclass(frozen=True)
class Cue:
    cue_id: str
    path: Path
    cooldown_ms: int


@dataclass
class CueBook:
    cues: dict[str, Cue] = field(default_factory=dict)
    _last: dict[str, float] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> CueBook:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CueBookError(f"no cue book at {path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise CueBookError(f"cannot read cue book {path} ({exc})") from exc
        if not isinstance(doc, dict) or doc.get("contractVersion") != 1:
            raise CueBookError(f"{path}: contractVersion must be 1")
        book = cls()
        for i, raw in enumerate(doc.get("cues") or []):
            if not isinstance(raw, dict):
                raise CueBookError(f"{path}: cue #{i} is not an object")
            missing = [k for k in _REQUIRED if k not in raw]
            if missing:
                raise CueBookError(f"{path}: cue #{i} lacks {', '.join(missing)}")
            prov = raw["provenance"]
            if (
                not isinstance(prov, dict)
                or not prov.get("origin")
                or not prov.get("date")
            ):
                raise CueBookError(
                    f"{path}: cue {raw['cueId']!r} has no provenance origin/date"
                )
            cue_id = str(raw["cueId"])
            if not _CUE_ID.match(cue_id):
                raise CueBookError(f"{path}: bad cueId {cue_id!r}")
            file = Path(str(raw["file"]))
            book.cues[cue_id] = Cue(
                cue_id=cue_id,
                path=file if file.is_absolute() else (path.parent / file).resolve(),
                cooldown_ms=max(0, int(raw.get("cooldownMs") or 0)),
            )
        return book

    def get(self, cue_id: str) -> Cue | None:
        return self.cues.get(cue_id)

    def ready(self, cue_id: str, now: float | None = None) -> bool:
        """True when the cue is out of cooldown (and marks it fired)."""
        cue = self.cues.get(cue_id)
        if cue is None:
            return False
        now = time.monotonic() if now is None else now
        last = self._last.get(cue_id)
        if last is not None and (now - last) * 1000 < cue.cooldown_ms:
            return False
        self._last[cue_id] = now
        return True
