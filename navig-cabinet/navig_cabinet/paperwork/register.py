"""The invoice register — the one place that answers "what did I bill, and was it paid?".

Two things make this more than a listing.

**It is rebuilt from the filed documents, but your annotations survive.** Everything
factual — number, date, client, amount — is re-derived from the PDFs on every run, so
the register cannot drift from the archive. Everything judgemental — whether an invoice
was actually paid, when, and any note — is read back from the existing register and
carried forward. Regenerating never costs you a payment status you recorded by hand.

**Unpaid is not the same as un-invoiced.** For a micro-entrepreneur the taxable event is
*encaissement* — money received, not money billed — so revenue for URSSAF and impôts is
the paid column, never the invoiced one. The register keeps them apart and totals both.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from .classify import _addressee, _amount, _client
from .extract import extract_text

from navig_sdk.host import command_name  # noqa: E402

# The command a user types here: `navig paperwork` inside navig, `navig-cabinet paperwork` on its own.
PAPERWORK_CMD = command_name("paperwork", standalone="navig-cabinet paperwork")

# Columns whose value is a human judgement, preserved verbatim across rebuilds.
PRESERVED = ("status", "paid_date", "notes")

STATUSES = ("paid", "unpaid", "overdue", "cancelled", "unknown")

COLUMNS = (
    "invoice_id", "date", "client", "amount_eur", "status", "paid_date",
    "path", "sha256", "notes",
)


@dataclass
class Entry:
    invoice_id: str = ""
    date: str = ""
    client: str = ""
    amount_eur: str = ""
    status: str = "unknown"
    paid_date: str = ""
    path: str = ""
    sha256: str = ""
    notes: str = ""

    def as_row(self) -> dict[str, str]:
        return {c: getattr(self, c) for c in COLUMNS}


@dataclass
class RegisterTotals:
    invoiced: float = 0.0
    paid: float = 0.0
    unpaid: float = 0.0
    unknown: float = 0.0
    count: int = 0
    by_client: dict[str, float] = field(default_factory=dict)


def _to_float(amount: str) -> float:
    """Parse a French or Anglo amount. Returns 0.0 when it cannot be read."""
    if not amount:
        return 0.0
    s = re.sub(r"[^\d.,]", "", amount)
    if not s:
        return 0.0
    # French: 1.234,56 — Anglo: 1,234.56. The LAST separator is the decimal one.
    last_comma, last_dot = s.rfind(","), s.rfind(".")
    if last_comma > last_dot:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def read_existing(path: Path) -> dict[str, dict[str, str]]:
    """Previous rows keyed by invoice id, so annotations survive a rebuild."""
    if not path.exists():
        return {}
    out: dict[str, dict[str, str]] = {}
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                key = (row.get("invoice_id") or "").strip()
                if key:
                    out[key] = row
    except OSError:
        return {}
    return out


def build(paths, docs, *, ocr: bool = True, max_pages: int = 2) -> list[Entry]:
    """One entry per filed issued invoice, with facts re-read from the document."""
    existing = read_existing(paths.space_root / "finance" / "invoices" / "register.csv")
    entries: list[Entry] = []

    for d in docs:
        if d.doc_class != "invoice-issued":
            continue
        full = paths.space_root / d.path
        text = extract_text(full, ocr=ocr, max_pages=max_pages).text if full.exists() else ""

        client = _client(text, full.name) or _addressee(text)
        if not client:
            # The canonical filename carries it: <date>-INV-NNNNNN-YY-<client>.pdf
            m = re.search(r"INV-\d{6}-\d{2}-(.+)$", full.stem)
            client = m.group(1) if m else ""

        e = Entry(
            invoice_id=d.doc_id,
            date=d.doc_date,
            client=client,
            amount_eur=_amount(text),
            path=d.path,
            sha256=d.sha256,
        )
        prev = existing.get(d.doc_id)
        if prev:
            for col in PRESERVED:
                if prev.get(col):
                    setattr(e, col, prev[col])
        entries.append(e)

    entries = _one_row_per_invoice(entries)
    entries.sort(key=lambda x: (x.invoice_id, x.date))
    return entries


def _one_row_per_invoice(entries: list[Entry]) -> list[Entry]:
    """Collapse several filed files of the same invoice into a single row.

    An invoice number can arrive as more than one file — `INV-000017-21.pdf` and
    `INV-000017-21-CYBESIS-STUDIOS.pdf` are both filed and both real. As register
    rows they are one invoice billed once, and duplicating it double-counts the
    revenue and makes the annotation key ambiguous.

    The richest row wins (most fields populated), and the extra paths are recorded in
    `notes` so nothing about the filing is hidden.
    """
    by_id: dict[str, list[Entry]] = {}
    for e in entries:
        by_id.setdefault(e.invoice_id, []).append(e)

    out: list[Entry] = []
    for invoice_id, group in by_id.items():
        if len(group) == 1:
            out.append(group[0])
            continue
        best = max(group, key=lambda x: sum(bool(getattr(x, c)) for c in COLUMNS))
        others = [g.path for g in group if g.path != best.path]
        if others:
            note = f"also filed as: {', '.join(sorted(others))}"
            best.notes = f"{best.notes}; {note}" if best.notes else note
        out.append(best)
    return out


def totals(entries: list[Entry]) -> RegisterTotals:
    t = RegisterTotals(count=len(entries))
    for e in entries:
        amount = _to_float(e.amount_eur)
        t.invoiced += amount
        status = (e.status or "unknown").lower()
        if status == "paid":
            t.paid += amount
            t.by_client[e.client or "—"] = t.by_client.get(e.client or "—", 0.0) + amount
        elif status in ("unpaid", "overdue"):
            t.unpaid += amount
        elif status != "cancelled":
            t.unknown += amount
    return t


def write(paths, entries: list[Entry]) -> RegisterTotals:
    """Persist ``register.csv`` (machine + Excel) and ``REGISTER.md`` (human)."""
    out_dir = paths.space_root / "finance" / "invoices"
    out_dir.mkdir(parents=True, exist_ok=True)

    csv_path = out_dir / "register.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(COLUMNS))
        w.writeheader()
        for e in entries:
            w.writerow(e.as_row())

    t = totals(entries)
    unpaid = [e for e in entries if (e.status or "").lower() in ("unpaid", "overdue")]
    unknown = [e for e in entries if (e.status or "unknown").lower() == "unknown"]

    lines = [
        "# Invoice register",
        "",
        f"Rebuilt from the filed invoices by `{PAPERWORK_CMD} index`. The factual columns",
        "are re-read from the documents every time; **`status`, `paid_date` and `notes`",
        "are yours and are preserved** — edit `register.csv` and they survive a rebuild.",
        "",
        f"- **Invoiced:** {t.invoiced:,.2f} EUR across {t.count} invoice(s)",
        f"- **Paid:** {t.paid:,.2f} EUR",
        f"- **Unpaid:** {t.unpaid:,.2f} EUR",
        f"- **Status not recorded:** {t.unknown:,.2f} EUR",
        "",
        "> For a micro-entrepreneur the taxable event is *encaissement* — money received.",
        "> Declare the **paid** figure, never the invoiced one.",
        "",
        "| Invoice | Date | Client | Amount EUR | Status | Paid |",
        "|---|---|---|---:|---|---|",
    ]
    for e in entries:
        status = (e.status or "unknown").lower()
        mark = {"paid": "✅ paid", "unpaid": "❌ unpaid", "overdue": "⚠️ overdue",
                "cancelled": "⛔ cancelled"}.get(status, "· not recorded")
        lines.append(
            f"| `{e.invoice_id}` | {e.date or '—'} | {e.client or '—'} | "
            f"{e.amount_eur or '—'} | {mark} | {e.paid_date or ''} |"
        )
    lines.append("")

    if unpaid:
        lines += ["## Not paid", ""]
        for e in unpaid:
            lines.append(f"- `{e.invoice_id}` — {e.client or '—'}, {e.amount_eur or '—'} EUR"
                         + (f" — {e.notes}" if e.notes else ""))
        lines.append("")
    if unknown:
        lines += [
            f"## Status not yet recorded — {len(unknown)}",
            "",
            "Set `status` to `paid` / `unpaid` in `register.csv`; the value survives rebuilds.",
            "",
        ]
        for e in unknown:
            lines.append(f"- `{e.invoice_id}` — {e.client or '—'}, {e.amount_eur or '—'} EUR")
        lines.append("")

    (out_dir / "REGISTER.md").write_text("\n".join(lines), encoding="utf-8")
    return t
