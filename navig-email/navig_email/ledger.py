"""JSONL ledgers under ``<space>/mailroom/ledger/``, idempotent by key.

A ledger row is a fact the space keeps about a thread or a document; re-running a
command upserts rather than appends, and hand-edited fields the new row does not
carry survive (the same contract as the paperwork mailroom).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Sequence


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def upsert(path: Path, new_rows: Sequence[dict], key: str) -> tuple[int, int]:
    """Insert or merge by *key*. Returns ``(added, updated)``."""
    existing = read_jsonl(path)
    index = {str(r.get(key)): i for i, r in enumerate(existing) if r.get(key)}
    added = updated = 0
    for row in new_rows:
        k = str(row.get(key) or "")
        if not k:
            continue
        if k in index:
            existing[index[k]] = {**existing[index[k]], **row}
            updated += 1
        else:
            existing.append(dict(row))
            index[k] = len(existing) - 1
            added += 1
    write_jsonl(path, existing)
    return added, updated
