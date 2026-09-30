"""One payload describing the whole mailroom — for the navig OS Mailroom tab.

Four independent sections, because they fail independently: the **edge** (the Cloudflare
Worker in front of the domain aliases), **gmail** (the connected mailbox, its watch state
and the spam-reply ledger), **paper** (letters filed by ``navig paperwork`` and the
deadline radar) and **cron** (the jobs that drive both channels unattended). A section that
cannot be read returns ``{"error": …}`` rather than failing the whole view — a mailbox that
is not connected yet must not hide the paper mail that is already filed.

Nothing here talks to a model, and no message body or dossier reference ever leaves: the
edge exposes metadata only, and the courrier ledger stores a hash of the reference.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("navig_email.overview")

EDGE_DAYS = 7
EDGE_EVENTS = 20
PAPER_ROWS = 20
RADAR_HORIZON = 30
#: Cron jobs that belong to the mailroom (by name prefix).
CRON_PREFIXES = ("mail:", "courrier:")


def _read_jsonl(path: Path, *, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    if limit:
        return rows[-limit:][::-1]
    return rows


# ── sections ────────────────────────────────────────────────────────────────


async def edge_section(paths) -> dict[str, Any]:
    from navig_email import edge as E

    try:
        url = E.edge_url(paths)
    except E.EdgeNotConfigured as exc:
        return {"configured": False, "error": str(exc)}
    out: dict[str, Any] = {"configured": True, "url": url}
    try:
        out["health"] = await asyncio.to_thread(E.health, paths)
        out["stats"] = await asyncio.to_thread(E.stats, paths, days=EDGE_DAYS)
        out["events"] = await asyncio.to_thread(E.events, paths, limit=EDGE_EVENTS)
        out["accounts_yaml_notify"] = E.notify_aliases(paths)
    except Exception as exc:  # noqa: BLE001 — the edge is one section, not the page
        out["error"] = str(exc)[:300]
    return out


async def gmail_section(paths) -> dict[str, Any]:
    from navig_email import account, state as S
    from navig_email.rules_file import load_rules

    out: dict[str, Any] = {}
    try:
        out["connected"] = account.is_connected()
        out["account"] = account.connected_email()
        out["linked"] = account.linked_accounts()
    except Exception as exc:  # noqa: BLE001
        out["connected"] = False
        out["error"] = str(exc)[:300]
    try:
        st = S.load(paths)
        out["watch"] = {
            "seeded": bool(st.get("seeded")),
            "last_watch": st.get("last_watch") or "",
            "last_error": st.get("last_error") or "",
            "seen": len(st.get("seen_ids") or []),
        }
    except Exception as exc:  # noqa: BLE001
        out["watch"] = {"error": str(exc)[:200]}
    try:
        out["rules"] = len(load_rules(paths))
        out["rules_file"] = str(paths.rules_yaml)
    except Exception as exc:  # noqa: BLE001
        out["rules"] = 0
        out["rules_error"] = str(exc)[:200]
    rows = _read_jsonl(paths.ledger("piratebay"))
    out["piratebay"] = {
        "total": len(rows),
        "answered_back": sum(1 for r in rows if r.get("answered_back")),
        "drafts": sum(1 for r in rows if r.get("draft_id")),
        "labelled": sum(1 for r in rows if r.get("labels_applied")),
    }
    return out


def _radar_from_jsonl(path: Path, *, horizon: int, today: date) -> list[dict[str, Any]]:
    """The deadline radar without importing navig-cabinet's paperwork filer (it may not be installed)."""
    out: list[dict[str, Any]] = []
    for row in _read_jsonl(path):
        if str(row.get("status", "open")).lower() != "open":
            continue
        try:
            due = date.fromisoformat(str(row.get("due"))[:10])
        except (TypeError, ValueError):
            continue
        days = (due - today).days
        if days > horizon:
            continue
        status = "EN RETARD" if days < 0 else ("URGENT" if days <= 7 else "À VENIR")
        out.append(
            {
                "due": due.isoformat(),
                "days": days,
                "status": status,
                "organisme": row.get("organisme") or "",
                "objet": row.get("objet") or "",
                "action": row.get("action") or "info",
                "source": row.get("source") or "",
            }
        )
    out.sort(key=lambda d: d["due"])
    return out


def paper_section(paths, *, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    root = paths.space_root
    base = root / "mailroom"
    out: dict[str, Any] = {"space": str(root)}
    try:
        out["filed"] = _read_jsonl(base / "ledger" / "courrier.jsonl", limit=PAPER_ROWS)
        out["filed_total"] = len(_read_jsonl(base / "ledger" / "courrier.jsonl"))
        out["radar"] = _radar_from_jsonl(
            base / "echeances.jsonl", horizon=RADAR_HORIZON, today=today
        )
        inbox = root / "inbox"
        pending = (
            [
                p.name
                for p in inbox.iterdir()
                if p.is_file() and not p.name.startswith(".")
            ]
            if inbox.is_dir()
            else []
        )
        out["inbox_pending"] = len(pending)
        out["inbox_files"] = pending[:10]
        report = base / "reports" / "echeancier.md"
        out["report"] = str(report) if report.exists() else ""
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:300]
    return out


def cron_section() -> dict[str, Any]:
    """The mailroom's cron jobs, straight from the scheduler's store (no gateway needed)."""
    try:
        from navig.platform import paths as npaths

        store = npaths.data_dir().parent / "scheduler" / "cron_jobs.json"
        if not store.exists():
            return {"jobs": [], "store": str(store)}
        data = json.loads(store.read_text(encoding="utf-8"))
        jobs = data.get("jobs") if isinstance(data, dict) else data
        out = []
        for job in jobs or []:
            name = str(job.get("name") or "")
            if not name.startswith(CRON_PREFIXES):
                continue
            out.append(
                {
                    "id": job.get("id") or "",
                    "name": name,
                    "schedule": job.get("schedule") or "",
                    "enabled": bool(job.get("enabled")),
                    "next_run": job.get("next_run") or "",
                    "last_run": job.get("last_run") or "",
                }
            )
        out.sort(key=lambda j: j["name"])
        return {"jobs": out, "store": str(store)}
    except Exception as exc:  # noqa: BLE001
        return {"jobs": [], "error": str(exc)[:200]}


# ── the payload ─────────────────────────────────────────────────────────────


def resolve_space(space: str | None):
    """``MailroomPaths`` for *space*, the global ``mailroom.paper_space``, or ``None``."""
    from navig_email.space import paths_for

    name = (space or "").strip()
    if not name:
        try:
            from navig.config import get_config_manager

            cfg = (get_config_manager().global_config or {}).get("mailroom") or {}
            name = str(cfg.get("paper_space") or "").strip()
        except Exception:  # noqa: BLE001
            name = ""
    if not name:
        return None
    return paths_for(name)


async def build_overview(
    space: str | None = None, *, today: date | None = None
) -> dict[str, Any]:
    """Everything the Mailroom view shows, in one round-trip."""
    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "space": "",
    }
    try:
        paths = resolve_space(space)
    except ValueError as exc:
        return {**payload, "error": str(exc)}
    if paths is None:
        return {
            **payload,
            "error": "no space — pass ?space=<name> or set mailroom.paper_space",
        }
    payload["space"] = str(paths.space_root)
    payload["space_name"] = paths.space_root.name
    edge, gmail = await asyncio.gather(edge_section(paths), gmail_section(paths))
    payload["edge"] = edge
    payload["gmail"] = gmail
    payload["paper"] = await asyncio.to_thread(paper_section, paths, today=today)
    payload["cron"] = await asyncio.to_thread(cron_section)
    return payload
