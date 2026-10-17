"""The mailroom: what the paperwork space keeps *about* its letters, and the radar.

Filing a letter under ``personal/<bucket>/`` answers "where is it". The mailroom
answers the two questions that come after: *what came in* (``ledger/courrier.jsonl``,
one row per filed document) and *what is due* (``echeances.jsonl`` plus a
hand-maintained ``recurrent.yaml`` of renewals, rendered by the radar into
``reports/echeancier.md`` and a Telegram line).

All of it lives under ``<space>/mailroom/`` — human-readable, next to the email
ledgers the ``navig email`` plugin writes into the same folder. Writes are
idempotent (upsert by document hash / deadline id) so a re-run never duplicates.

Privacy: the ledger stores ``reference_hash`` (see ``courrier.py``), never the raw
dossier reference, and the Telegram text never carries a reference at all.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from .courrier import organism_label, reference_hash
from .plan import PlanRow
from .space import PaperworkPaths

MAILROOM_DIR = "mailroom"

# Deadline statuses, in the words the space's own runbooks use.
EN_RETARD = "EN RETARD"
URGENT = "URGENT"
A_VENIR = "À VENIR"
URGENT_DAYS = 7


@dataclass(frozen=True)
class MailroomPaths:
    space_root: Path

    @property
    def base(self) -> Path:
        return self.space_root / MAILROOM_DIR

    @property
    def ledger_dir(self) -> Path:
        return self.base / "ledger"

    @property
    def courrier_jsonl(self) -> Path:
        return self.ledger_dir / "courrier.jsonl"

    @property
    def echeances_jsonl(self) -> Path:
        return self.base / "echeances.jsonl"

    @property
    def recurrent_yaml(self) -> Path:
        return self.base / "recurrent.yaml"

    @property
    def reports_dir(self) -> Path:
        return self.base / "reports"

    @property
    def echeancier_md(self) -> Path:
        return self.reports_dir / "echeancier.md"


def mailroom_paths(paths: PaperworkPaths) -> MailroomPaths:
    return MailroomPaths(paths.space_root)


# ── JSONL, idempotent ───────────────────────────────────────────────────────


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
            continue  # a damaged line is skipped, never fatal, never rewritten as data
        if isinstance(item, dict):
            out.append(item)
    return out


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    """Whole-file rewrite through a temp file — a crash cannot truncate the ledger."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def upsert_jsonl(path: Path, new_rows: Sequence[dict], key: str) -> int:
    """Insert or replace *new_rows* by *key*. Returns how many were new."""
    existing = read_jsonl(path)
    by_key = {str(r.get(key)): i for i, r in enumerate(existing) if r.get(key)}
    added = 0
    for row in new_rows:
        k = str(row.get(key) or "")
        if not k:
            continue
        if k in by_key:
            # Keep hand-edited fields (status, notes) that the new row does not carry.
            merged = {**existing[by_key[k]], **row}
            existing[by_key[k]] = merged
        else:
            existing.append(row)
            by_key[k] = len(existing) - 1
            added += 1
    write_jsonl(path, existing)
    return added


# ── Recording what was filed ────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def migrated_row_ids(receipt: Path | None) -> set[str]:
    """Row ids the receipt says were actually migrated in this run."""
    if receipt is None or not receipt.exists():
        return set()
    ids: set[str] = set()
    for entry in read_jsonl(receipt):
        if entry.get("action") == "migrated" and entry.get("row_id"):
            ids.add(str(entry["row_id"]))
    return ids


def ledger_entry(row: PlanRow) -> dict:
    return {
        "sha256": row.sha256,
        "row_id": row.row_id,
        "filed_at": _now(),
        "dest_rel": row.dest_rel,
        "original_name": row.original_name,
        "doc_class": row.doc_class,
        "bucket": row.doc_class.removeprefix("courrier-")
        if row.doc_class.startswith("courrier-")
        else "",
        "emetteur": row.emetteur,
        "emetteur_label": organism_label(row.emetteur),
        "doc_date": row.doc_date,
        "echeance": row.echeance,
        "action": row.action_requise,
        "reference_hash": reference_hash(row.reference),
        "channel": row.channel,
        "confidence": row.confidence,
        "source": "paper",
        "profile": row.profile or "personal",
    }


def echeance_entry(row: PlanRow) -> dict | None:
    if not row.echeance:
        return None
    dest_stem = (
        Path(row.dest_rel).stem if row.dest_rel else Path(row.original_name).stem
    )
    return {
        "id": (row.sha256 or row.row_id)[:12],
        "due": row.echeance,
        "organisme": organism_label(row.emetteur) or row.emetteur or "",
        "objet": row.objet or dest_stem,
        "action": row.action_requise or "info",
        "source": row.dest_rel or row.original_name,
        "status": "open",
        "created_at": _now(),
    }


def record_filed(
    rows: Sequence[PlanRow],
    paths: PaperworkPaths,
    receipt: Path | None,
) -> tuple[list[PlanRow], int, int]:
    """Upsert the ledger and the échéances for the rows the receipt says were filed.

    Returns ``(filed_rows, new_ledger_entries, new_deadlines)``.
    """
    done = migrated_row_ids(receipt)
    filed = [
        r for r in rows if r.row_id in done and r.doc_class.startswith("courrier-")
    ]
    if not filed:
        return [], 0, 0
    mp = mailroom_paths(paths)
    n_ledger = upsert_jsonl(
        mp.courrier_jsonl, [ledger_entry(r) for r in filed], key="sha256"
    )
    deadlines = [e for e in (echeance_entry(r) for r in filed) if e]
    n_due = upsert_jsonl(mp.echeances_jsonl, deadlines, key="id") if deadlines else 0
    return filed, n_ledger, n_due


# ── The radar ───────────────────────────────────────────────────────────────


@dataclass
class Deadline:
    id: str
    due: date
    organisme: str
    objet: str
    action: str
    source: str
    days: int
    status: str
    recurrent: bool = False


def _status(days: int) -> str:
    if days < 0:
        return EN_RETARD
    if days <= URGENT_DAYS:
        return URGENT
    return A_VENIR


def _parse_due(value: object) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def read_recurrent(path: Path) -> list[dict]:
    """``recurrent.yaml`` → ``[{id, objet, organisme, due, action?}]``; ``[]`` if absent."""
    if not path.exists():
        return []
    try:
        from navig_sdk.files import safe_load_yaml  # navig's reader, or its twin standalone

        data = safe_load_yaml(path)
    except Exception:  # noqa: BLE001 — a broken file contributes nothing, loudly enough in the report
        return []
    items = data.get("recurrent") if isinstance(data, dict) else None
    return [i for i in (items or []) if isinstance(i, dict)]


def radar(
    paths: PaperworkPaths,
    *,
    horizon_days: int = 30,
    today: date | None = None,
) -> list[Deadline]:
    """Every open deadline that is overdue or due within *horizon_days*, soonest first."""
    today = today or date.today()
    mp = mailroom_paths(paths)
    out: list[Deadline] = []

    for e in read_jsonl(mp.echeances_jsonl):
        if str(e.get("status", "open")).lower() != "open":
            continue
        due = _parse_due(e.get("due"))
        if due is None:
            continue
        days = (due - today).days
        if days > horizon_days:
            continue
        out.append(
            Deadline(
                id=str(e.get("id", "")),
                due=due,
                organisme=str(e.get("organisme", "")),
                objet=str(e.get("objet", "")),
                action=str(e.get("action", "info")),
                source=str(e.get("source", "")),
                days=days,
                status=_status(days),
            )
        )

    for r in read_recurrent(mp.recurrent_yaml):
        due = _parse_due(r.get("due"))
        if due is None:
            continue
        days = (due - today).days
        if days > horizon_days:
            continue
        out.append(
            Deadline(
                id=str(r.get("id") or r.get("objet") or ""),
                due=due,
                organisme=str(r.get("organisme", "")),
                objet=str(r.get("objet", "")),
                action=str(r.get("action", "renouveler")),
                source="recurrent.yaml",
                days=days,
                status=_status(days),
                recurrent=True,
            )
        )

    out.sort(key=lambda d: (d.due, d.organisme, d.objet))
    return out


def write_echeancier(
    paths: PaperworkPaths,
    items: Sequence[Deadline],
    *,
    horizon_days: int,
    today: date | None = None,
) -> Path:
    today = today or date.today()
    mp = mailroom_paths(paths)
    mp.reports_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Échéancier",
        "",
        f"Généré le {today.isoformat()} — horizon {horizon_days} jours. "
        f"Sources : `mailroom/echeances.jsonl` (courriers filés) et `mailroom/recurrent.yaml` (renouvellements).",
        "",
    ]
    if not items:
        lines += ["Aucune échéance dans l'horizon.", ""]
    else:
        top = items[:3]
        lines += ["## Top 3 urgences", ""]
        for d in top:
            lines.append(
                f"- **{d.status}** · {d.due.isoformat()} · {d.organisme or '—'} · {d.objet} ({d.days} j)"
            )
        lines += [
            "",
            "| Échéance | Organisme | Objet | Jours restants | Statut | Action | Source |",
            "|---|---|---|---|---|---|---|",
        ]
        for d in items:
            lines.append(
                f"| {d.due.isoformat()} | {d.organisme or '—'} | {d.objet} | {d.days} | {d.status} | "
                f"{d.action} | `{d.source}` |"
            )
        lines.append("")
    mp.echeancier_md.write_text("\n".join(lines), encoding="utf-8")
    return mp.echeancier_md


# ── Telegram text (HTML parse mode; callers escape nothing else) ────────────


def _esc(text: str) -> str:
    from navig_cabinet._core import escape_html

    return escape_html(text)


def telegram_filed_text(filed: Sequence[PlanRow]) -> str:
    """One line per filed letter: émetteur · objet · échéance → destination. No references."""
    if not filed:
        return ""
    lines = [f"📬 <b>Courrier classé</b> — {len(filed)} document(s)"]
    for r in filed:
        who = organism_label(r.emetteur) or "Émetteur inconnu"
        objet = r.objet or Path(r.original_name).stem
        due = f" · échéance <b>{r.echeance}</b>" if r.echeance else ""
        action = (
            f" · {r.action_requise}"
            if r.action_requise and r.action_requise != "info"
            else ""
        )
        dest = str(Path(r.dest_rel).parent.as_posix()) if r.dest_rel else "?"
        lines.append(
            f"• {_esc(who)} · {_esc(objet[:80])}{due}{action} → <code>{_esc(dest)}/</code>"
        )
    return "\n".join(lines)


def telegram_radar_text(items: Sequence[Deadline], *, horizon_days: int) -> str:
    if not items:
        return f"🗓 <b>Échéances</b> — rien dans les {horizon_days} prochains jours."
    icon = {EN_RETARD: "🔴", URGENT: "🟠", A_VENIR: "🟢"}
    lines = [f"🗓 <b>Échéances</b> — {len(items)} dans les {horizon_days} jours"]
    for d in items[:15]:
        when = f"J{d.days:+d}" if d.days else "aujourd'hui"
        lines.append(
            f"{icon.get(d.status, '•')} {d.due.isoformat()} ({when}) · {_esc(d.organisme or '—')} · "
            f"{_esc(d.objet[:70])}"
        )
    if len(items) > 15:
        lines.append(f"… +{len(items) - 15} — voir mailroom/reports/echeancier.md")
    return "\n".join(lines)
