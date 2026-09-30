"""Reverse an apply, from its receipt. The recovery half of the migration contract.

One rule carries the weight: **a destination whose content changed since it was
migrated is never deleted.** If the hash no longer matches the receipt, the document
was edited in place after filing, and removing it would destroy work that exists
nowhere else. Such an entry is refused, reported, and the rest of the reversal
continues.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .apply import _sha256
from .space import PaperworkPaths


@dataclass
class UndoStats:
    restored: int = 0
    already: int = 0
    refused: int = 0
    missing: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.refused or self.errors)


def latest_receipt(paths: PaperworkPaths) -> Path | None:
    if not paths.receipts_dir.is_dir():
        return None
    receipts = sorted(paths.receipts_dir.glob("apply-*.jsonl"))
    return receipts[-1] if receipts else None


def undo(receipt: Path, *, dry_run: bool = False) -> UndoStats:
    """Replay *receipt* in reverse. Idempotent: a already-restored entry is a no-op."""
    st = UndoStats()
    entries = []
    with receipt.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    st.errors.append(f"unreadable receipt line: {exc}")

    for entry in reversed(entries):
        src = Path(entry.get("src", ""))
        dest = Path(entry["dest"]) if entry.get("dest") else None
        trash = Path(entry["trash"]) if entry.get("trash") else None
        recorded = entry.get("sha256", "")

        if src.exists() and (dest is None or not dest.exists()):
            st.already += 1
            continue

        if dest is not None and dest.exists() and recorded:
            if _sha256(dest) != recorded:
                st.refused += 1
                st.errors.append(
                    f"{dest} changed since it was filed — left in place rather than deleted"
                )
                continue

        if trash is None or not trash.exists():
            st.missing += 1
            continue

        if dry_run:
            st.restored += 1
            continue

        try:
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(trash), str(src))
            if dest is not None and dest.exists():
                dest.unlink()
            st.restored += 1
        except OSError as exc:
            st.errors.append(f"{src}: {exc}")

    return st
