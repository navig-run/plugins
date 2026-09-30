"""Mailbox statistics for a period — pure counting over Gmail ids. No model.

Counts come from ``messages.list`` (ids only, 500 a page) with an ``after:/before:``
epoch window; per-sender and per-day figures come from one metadata pass over the
inbox ids of the period (capped). Everything is deterministic and cheap enough to
run monthly on a mailbox with tens of thousands of messages.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import messages, rules as R

PERIODS = ("day", "week", "month")
META_CAP = 500  # inbox messages read for senders / per-day, per period


@dataclass(frozen=True)
class Period:
    name: str
    since: date  # inclusive
    until: date  # exclusive

    @property
    def label(self) -> str:
        last = self.until - timedelta(days=1)
        return f"{self.since.isoformat()} → {last.isoformat()}"

    def query(self) -> str:
        return f"after:{_epoch(self.since)} before:{_epoch(self.until)}"


def _epoch(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def period_bounds(
    name: str,
    *,
    since: date | None = None,
    until: date | None = None,
    today: date | None = None,
) -> Period:
    """Rolling windows ending today (inclusive): day = today, week = 7 days, month = 30.
    ``since``/``until`` (inclusive dates) override the window."""
    today = today or date.today()
    if since or until:
        s = since or (until or today) - timedelta(days=29)
        u = (until or today) + timedelta(days=1)
        return Period("custom", s, u)
    if name not in PERIODS:
        raise ValueError(f"period must be one of {', '.join(PERIODS)}")
    days = {"day": 1, "week": 7, "month": 30}[name]
    return Period(name, today - timedelta(days=days - 1), today + timedelta(days=1))


# What gets counted. (key, label, gmail query, include spam/trash)
COUNTS: tuple[tuple[str, str, str, bool], ...] = (
    ("received", "Reçus (hors envoyés)", "-in:sent -in:draft -in:chat", True),
    ("inbox", "Boîte de réception", "in:inbox", False),
    ("unread", "Non lus (réception)", "in:inbox is:unread", False),
    ("sent", "Envoyés", "in:sent", False),
    ("spam", "Spam", "in:spam", True),
    ("starred", "Suivis", "is:starred", False),
    ("attachments", "Avec pièce jointe", "has:attachment -in:sent", True),
)


async def compute(
    connector,
    period: Period,
    *,
    rule_list: list[dict[str, Any]] | None = None,
    top: int = 10,
) -> dict[str, Any]:
    window = period.query()
    counts: dict[str, int] = {}
    for key, _label, q, spam in COUNTS:
        entries = await connector.iter_message_ids(
            f"{q} {window}", include_spam_trash=spam
        )
        counts[key] = len(entries)

    # Per rule-label and per `to:` rule (the support alias), from the rules file.
    labels_by_name = {
        str(lbl.get("name", "")).lower(): str(lbl.get("id", ""))
        for lbl in await connector.list_labels()
    }
    label_counts: dict[str, int] = {}
    rule_counts: dict[str, int] = {}
    for rule in rule_list or []:
        try:
            acts = R.actions_for(rule)
        except ValueError:
            acts = []
        for act in acts:
            if (
                act.kind == "label"
                and act.arg.lower() in labels_by_name
                and act.arg not in label_counts
            ):
                entries = await connector.iter_message_ids(
                    window,
                    label_ids=[labels_by_name[act.arg.lower()]],
                    include_spam_trash=True,
                )
                label_counts[act.arg] = len(entries)
        n = R.normalize(rule)
        rid = str(n.get("id") or n.get("name") or "")
        if n.get("to"):
            entries = await connector.iter_message_ids(
                f"to:{n['to']} {window}", include_spam_trash=True
            )
            rule_counts[rid] = len(entries)

    # Senders and per-day: one metadata pass over the inbox of the period.
    inbox_ids = [
        e["id"]
        for e in await connector.iter_message_ids(f"in:inbox {window}", limit=META_CAP)
    ]
    raw = await connector.get_many(
        inbox_ids, format="metadata", headers=["From", "Subject"]
    )
    senders: Counter[str] = Counter()
    domains: Counter[str] = Counter()
    per_day: Counter[str] = Counter()
    for m in raw:
        s = messages.shape(m)
        addr = messages.address(s["from"])
        if addr:
            senders[addr] += 1
            domains[messages.domain_of(addr)] += 1
        if s["internal_ms"]:
            per_day[
                datetime.fromtimestamp(s["internal_ms"] / 1000, tz=timezone.utc)
                .date()
                .isoformat()
            ] += 1

    return {
        "period": {
            "name": period.name,
            "since": period.since.isoformat(),
            "until": (period.until - timedelta(days=1)).isoformat(),
            "label": period.label,
        },
        "counts": counts,
        "labels": label_counts,
        "rules": rule_counts,
        "top_senders": senders.most_common(top),
        "top_domains": domains.most_common(top),
        "per_day": dict(sorted(per_day.items())),
        "sampled_inbox": len(raw),
        "sample_capped": len(inbox_ids) >= META_CAP,
    }


def render_markdown(stats: dict[str, Any], *, account: str = "") -> str:
    p = stats["period"]
    c = stats["counts"]
    lines = [
        f"# Statistiques mail — {p['label']}",
        "",
        f"Compte : `{account or '?'}` · période `{p['name']}` · généré le {date.today().isoformat()}",
        "",
        "| Mesure | Nombre |",
        "|---|---|",
    ]
    for key, label, _q, _s in COUNTS:
        lines.append(f"| {label} | {c.get(key, 0)} |")
    if stats.get("labels"):
        lines += [
            "",
            "## Par étiquette (règles)",
            "",
            "| Étiquette | Nombre |",
            "|---|---|",
        ]
        for name, n in sorted(stats["labels"].items(), key=lambda kv: -kv[1]):
            lines.append(f"| {name} | {n} |")
    if stats.get("rules"):
        lines += [
            "",
            "## Par règle (destinataire)",
            "",
            "| Règle | Nombre |",
            "|---|---|",
        ]
        for name, n in sorted(stats["rules"].items(), key=lambda kv: -kv[1]):
            lines.append(f"| {name} | {n} |")
    if stats.get("top_senders"):
        cap = " (échantillon plafonné)" if stats.get("sample_capped") else ""
        lines += [
            "",
            f"## Expéditeurs les plus fréquents{cap}",
            "",
            "| Expéditeur | Messages |",
            "|---|---|",
        ]
        for addr, n in stats["top_senders"]:
            lines.append(f"| {addr} | {n} |")
        lines += ["", "| Domaine | Messages |", "|---|---|"]
        for dom, n in stats["top_domains"]:
            lines.append(f"| {dom} | {n} |")
    if stats.get("per_day"):
        lines += ["", "## Par jour (réception)", "", "| Jour | Messages |", "|---|---|"]
        for day, n in stats["per_day"].items():
            lines.append(f"| {day} | {n} |")
    lines.append("")
    return "\n".join(lines)


def telegram_text(stats: dict[str, Any], *, title: str = "Statistiques mail") -> str:
    from navig_email._compat import escape_html as esc

    p = stats["period"]
    c = stats["counts"]
    lines = [
        f"📊 <b>{esc(title)}</b> — {esc(p['label'])}",
        f"Reçus <b>{c.get('received', 0)}</b> · réception {c.get('inbox', 0)} · non lus {c.get('unread', 0)} · "
        f"envoyés <b>{c.get('sent', 0)}</b> · spam {c.get('spam', 0)} · pièces jointes {c.get('attachments', 0)}",
    ]
    if stats.get("labels") or stats.get("rules"):
        bits = [
            f"{esc(k)} {v}"
            for k, v in {**stats.get("rules", {}), **stats.get("labels", {})}.items()
        ]
        lines.append("Règles : " + " · ".join(bits))
    if stats.get("top_domains"):
        lines.append(
            "Top domaines : "
            + ", ".join(f"{esc(d)} ({n})" for d, n in stats["top_domains"][:5])
        )
    return "\n".join(lines)
