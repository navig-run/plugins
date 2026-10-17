"""The document index, derived from disk — never a second source of truth.

``index`` re-walks the filed tree, re-hashes, and re-parses the canonical names. That
is deliberate: a ledger maintained only by ``apply`` would drift the moment anyone
moved a file by hand, and a drifted ledger is worse than none, because it is believed.

It also answers the question the space cannot currently answer for itself: **what is
the next invoice number?** The space's ``generate-invoice`` skill globs
``finance/invoices/CYB-{year}-*.md``, which matches nothing here, so it would mint
``CYB-2026-001`` alongside a real series that has reached ``INV-000035-26``. Under the
French sequential-numbering rule that is a broken chain. This module computes the true
next id; correcting the skill is a separate, owner-confirmed change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

from . import signals as S
from .apply import _sha256
from .space import PaperworkPaths

# Trees this plugin files into. Anything else in the space is not its business.
INDEXED_TREES = (
    "finance/invoices",
    "finance/quotes",
    "finance/tax",
    "finance/expenses",
    "finance/reports",
    "contracts",
    "clients",
    "ops/legal",
    "ops/payroll",
    "docs/unsorted",
)


@dataclass
class Document:
    doc_id: str
    doc_class: str
    path: str
    sha256: str
    doc_date: str
    size: int
    series_n: int | None = None
    series_yy: str | None = None

    def as_dict(self) -> dict:
        d = {
            "doc_id": self.doc_id,
            "doc_class": self.doc_class,
            "path": self.path,
            "sha256": self.sha256,
            "doc_date": self.doc_date,
            "size": self.size,
        }
        if self.series_n is not None:
            d["series_n"] = self.series_n
            d["series_yy"] = self.series_yy
        return d


def _class_for(rel: str) -> str:
    if rel.startswith("finance/invoices/issued"):
        return "invoice-issued"
    if rel.startswith("finance/invoices/received"):
        return "invoice-received"
    if rel.startswith("finance/quotes"):
        return "quote-issued"
    if rel.startswith("finance/tax"):
        return "tax-social"
    if rel.startswith("finance/reports/bank"):
        return "bank"
    if rel.startswith("finance/expenses"):
        return "invoice-received"
    if rel.startswith("contracts"):
        return "contract"
    if rel.startswith("clients"):
        return "client-material"
    if rel.startswith("ops/legal"):
        return "company-legal"
    if rel.startswith("ops/payroll"):
        return "payroll-employment"
    return "unknown"


def _date_from_name(stem: str) -> str:
    return stem[:10] if len(stem) >= 10 and stem[4] == "-" and stem[7] == "-" else ""


def build(paths: PaperworkPaths) -> list[Document]:
    """Walk the filed trees and rebuild the document list from what is actually there."""
    docs: list[Document] = []
    for tree in INDEXED_TREES:
        root = paths.space_root / tree
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.name.startswith("."):
                continue
            rel = p.relative_to(paths.space_root).as_posix()
            m = S.INV_SERIES.search(p.stem)
            docs.append(
                Document(
                    doc_id=m.group(0) if m else p.stem,
                    doc_class=_class_for(rel),
                    path=rel,
                    sha256=_sha256(p),
                    doc_date=_date_from_name(p.stem),
                    size=p.stat().st_size,
                    series_n=int(m.group(1)) if m else None,
                    series_yy=m.group(2) if m else None,
                )
            )
    return docs


def next_invoice_id(docs: list[Document], *, today: date | None = None) -> str:
    """The next number in the real ``INV-NNNNNN-YY`` series.

    The sequence is continuous across years — it runs 17 (2021) to 35 (2026) without
    resetting — so the counter comes from the maximum, and only the year suffix moves.
    """
    numbers = [d.series_n for d in docs if d.series_n is not None]
    nxt = (max(numbers) + 1) if numbers else 1
    yy = (today or date.today()).strftime("%y")
    return f"INV-{nxt:06d}-{yy}"


def series_gaps(docs: list[Document]) -> list[int]:
    """Missing numbers inside the issued series. French law wants none."""
    numbers = sorted({d.series_n for d in docs if d.series_n is not None})
    if not numbers:
        return []
    return [n for n in range(numbers[0], numbers[-1] + 1) if n not in set(numbers)]


def write(paths: PaperworkPaths, docs: list[Document]) -> dict:
    """Persist ``documents.jsonl`` + ``index.md``. Returns a small summary."""
    paths.base.mkdir(parents=True, exist_ok=True)
    nxt = next_invoice_id(docs)
    gaps = series_gaps(docs)

    with paths.documents_jsonl.open("w", encoding="utf-8") as f:
        f.write(json.dumps(
            {"_header": True, "count": len(docs), "next_invoice_id": nxt, "series_gaps": gaps},
            ensure_ascii=False,
        ) + "\n")
        for d in docs:
            f.write(json.dumps(d.as_dict(), ensure_ascii=False) + "\n")

    by_class: dict[str, list[Document]] = {}
    for d in docs:
        by_class.setdefault(d.doc_class, []).append(d)

    lines = [
        "# Document index",
        "",
        f"{len(docs)} document(s) filed. Next invoice number: **{nxt}**.",
        "",
    ]
    if gaps:
        rendered = ", ".join(f"INV-{n:06d}" for n in gaps)
        lines += [
            f"> **Gap in the invoice series:** {rendered} missing. French invoicing "
            "requires a continuous, gapless sequence — worth resolving.",
            "",
        ]
    for cls in sorted(by_class):
        items = by_class[cls]
        lines += [f"## {cls} — {len(items)}", ""]
        for d in sorted(items, key=lambda x: (x.doc_date, x.path)):
            when = d.doc_date or "undated"
            lines.append(f"- `{when}` [{d.doc_id}]({d.path})")
        lines.append("")

    paths.index_md.write_text("\n".join(lines), encoding="utf-8")
    return {"count": len(docs), "next_invoice_id": nxt, "series_gaps": gaps}
