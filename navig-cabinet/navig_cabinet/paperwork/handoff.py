"""The manifest for documents that are not the company's.

This plugin can only write inside the space it was pointed at, and a person's medical
records or ID scans do not belong in a company space — but neither should they be
silently ignored, because then nobody ever learns they were triaged. So they are
*named*: what the file is, why it was vetoed, and which space or tool should own it.

The manifest never creates a directory, never copies, and never touches a sibling
space. It is a list, and acting on it is a separate, deliberate act performed from
the destination space.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Sequence

from ..ingest import PAPERWORK_CATEGORIES as _PERSONAL  # the subclasses the cabinet takes
from . import signals as S
from .plan import PlanRow

from navig_sdk.host import command_name  # noqa: E402

# The command a user types here: `navig cabinet` inside navig, `navig-cabinet` on its own.
CMD = command_name("cabinet", standalone="navig-cabinet")
PAPERWORK_CMD = command_name("paperwork", standalone="navig-cabinet paperwork")

_SUBCLASS_LABEL = {
    "health": "Health and medical",
    "health-domain": "Health and medical (by vocabulary)",
    "identity": "Personal identity",
    "benefits": "Benefits and social",
    "housing": "Housing",
    "thirdparty": "Another person's document",
    "secret": "Live credentials",
}


def rows_for(rows: Sequence[PlanRow]) -> list[PlanRow]:
    return [r for r in rows if r.decision == "handoff"]


def entry_for(row: PlanRow) -> dict:
    target = S.HANDOFF_MAP.get(row.veto_subclass or "", {})
    return {
        "row_id": row.row_id,
        "src": row.src,
        "doc_class": row.doc_class,
        "subclass": row.veto_subclass,
        "suggested_space": target.get("space"),
        "suggested_path": target.get("path"),
        "suggested_tool": target.get("tool"),
        "reason": row.signals,
        "confidence": row.confidence,
        "sha256": row.sha256,
        "size": row.size,
        "original_name": row.original_name,
        # Structural, not advisory: nothing downstream may give this row a destination.
        "never_migrate": True,
    }


def write(rows: Sequence[PlanRow], jsonl: Path, markdown: Path) -> int:
    """Write both manifest forms. Returns the number of entries."""
    entries = [entry_for(r) for r in rows_for(rows)]
    jsonl.parent.mkdir(parents=True, exist_ok=True)

    with jsonl.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    grouped: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        grouped[e["subclass"] or "unknown"].append(e)

    lines = [
        "# Handoff manifest",
        "",
        "Documents found during the paperwork scan that do **not** belong to the company.",
        "Nothing here was moved, copied or modified — every file is still where it was found.",
        "",
        f"**{len(entries)} documents** across {len(grouped)} categories.",
        "",
    ]
    if any(e["subclass"] in _PERSONAL for e in entries):
        # ID scans and medical records are documents a person keeps, so they are
        # encrypted into the cabinet rather than filed as plain files anywhere.
        lines += [
            "> **ID and medical documents** go into the encrypted cabinet: "
            f"`{PAPERWORK_CMD} handoff --yes` (or `{CMD} import-paperwork "
            f"{jsonl.as_posix()}`). Originals stay where they are until you delete them.",
            "",
        ]
    for subclass in sorted(grouped):
        items = grouped[subclass]
        target = S.HANDOFF_MAP.get(subclass, {})
        where = target.get("tool") or target.get("space") or "no destination — review by hand"
        path_hint = target.get("path") or ""
        lines += [
            f"## {_SUBCLASS_LABEL.get(subclass, subclass)} — {len(items)} file(s)",
            "",
            f"Belongs in: **{where}**{(' `' + path_hint + '`') if path_hint else ''}",
            "",
            "| File | Size | Found at |",
            "|---|---|---|",
        ]
        for e in sorted(items, key=lambda x: x["original_name"].lower()):
            size_kb = f"{e['size'] / 1024:.0f} KB"
            folder = str(Path(e["src"]).parent)
            lines.append(f"| `{e['original_name']}` | {size_kb} | `{folder}` |")
        lines.append("")

    markdown.write_text("\n".join(lines), encoding="utf-8")
    return len(entries)
