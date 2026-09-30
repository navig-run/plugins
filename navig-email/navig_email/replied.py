"""``navig email replied`` — the threads where the account replied to mail in a folder.

Built for one job: find every reply the operator sent to Spam-folder mail (the
Pirate-Bay corpus), ledger it, label it, and see who answered back. It reads Sent
outward, not Spam inward, because Gmail purges Spam after 30 days while the Sent
reply — with the quoted original — survives.

Tiers, deterministic:

* **confirmed** — the thread still holds a non-mine message carrying the folder's
  label (``SPAM``) dated before my first reply.
* **likely** — the original is gone: my first reply is the thread's first message,
  it has an ``In-Reply-To`` header, and its body carries the studio sign-off.
* everything else is an ordinary conversation and is skipped.

``answered_back`` is any non-mine message dated after my first reply.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import messages
from .messages import FOLDER_LABEL

TIER_CONFIRMED = "confirmed"
TIER_LIKELY = "likely"

# The reply style signs every message this way (see the space's style prompt).
SIGNOFF = re.compile(r"cybesis\s+studios", re.I)


@dataclass
class RepliedThread:
    thread_id: str
    tier: str
    sender: str = ""  # who I replied to (address)
    sender_name: str = ""
    sender_domain: str = ""
    subject: str = ""
    original_id: str = ""
    original_at: str = ""
    original_snippet: str = ""
    original_message_id: str = ""
    my_reply_id: str = ""
    my_reply_at: str = ""
    my_reply_text: str = ""
    replies_by_me: int = 0
    answered_back: bool = False
    answer_at: str = ""
    answer_snippet: str = ""
    answer_message_id: str = ""
    labels_applied: list[str] = field(default_factory=list)
    draft_id: str = ""
    url: str = ""

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["month"] = (self.my_reply_at or "")[:7]
        return d


def _epoch(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def classify_thread(
    msgs: list[dict[str, Any]], *, folder_label: str, my_email: str
) -> RepliedThread | None:
    """Pure: shaped messages (any order) → a RepliedThread, or None to skip."""
    msgs = sorted(msgs, key=lambda m: m.get("internal_ms", 0))
    mine = [m for m in msgs if messages.is_mine(m, my_email)]
    others = [m for m in msgs if not messages.is_mine(m, my_email)]
    if not mine:
        return None
    first_reply = mine[0]
    original = next(
        (
            m
            for m in others
            if folder_label in (m.get("labels") or [])
            and m["internal_ms"] <= first_reply["internal_ms"]
        ),
        None,
    )
    if original is not None:
        tier = TIER_CONFIRMED
    elif first_reply.get("in_reply_to") and all(
        o["internal_ms"] > first_reply["internal_ms"] for o in others
    ):
        tier = TIER_LIKELY  # original purged; body sign-off is checked by the caller
    else:
        return None

    later = [o for o in others if o["internal_ms"] > first_reply["internal_ms"]]
    sender_raw = original["from"] if original else first_reply["to"]
    rt = RepliedThread(
        thread_id=first_reply.get("thread_id")
        or (original or first_reply).get("thread_id", ""),
        tier=tier,
        sender=messages.address(sender_raw),
        sender_name=messages.display_name(sender_raw),
        sender_domain=messages.domain_of(sender_raw),
        subject=messages.bare_subject((original or first_reply).get("subject", "")),
        original_id=original["id"] if original else "",
        original_at=original["date"] if original else "",
        original_snippet=(original.get("snippet") or "")[:300] if original else "",
        original_message_id=original.get("message_id", "")
        if original
        else first_reply.get("in_reply_to", ""),
        my_reply_id=first_reply["id"],
        my_reply_at=first_reply["date"],
        replies_by_me=len(mine),
        answered_back=bool(later),
        url=f"https://mail.google.com/mail/#all/{first_reply.get('thread_id', '')}",
    )
    if later:
        rt.answer_at = later[0]["date"]
        rt.answer_snippet = (later[0].get("snippet") or "")[:300]
        rt.answer_message_id = later[0].get("message_id", "")
    return rt


async def find(
    connector,
    *,
    since: date,
    folder: str = "spam",
    my_email: str,
    concurrency: int = 8,
) -> list[RepliedThread]:
    folder_label = FOLDER_LABEL.get(folder.lower(), folder.upper())
    entries = await connector.iter_message_ids(f"in:sent after:{_epoch(since)}")
    thread_ids = list(
        dict.fromkeys(e["threadId"] for e in entries if e.get("threadId"))
    )
    threads = await connector.get_threads(
        thread_ids, format="metadata", concurrency=concurrency
    )

    found: list[RepliedThread] = []
    for t in threads:
        shaped = [messages.shape(m) for m in t.get("messages") or []]
        rt = classify_thread(shaped, folder_label=folder_label, my_email=my_email)
        if rt:
            found.append(rt)

    # One full fetch per thread — my first reply — for the corpus text and the
    # sign-off check that promotes/demotes the `likely` tier.
    full = await connector.get_many(
        [rt.my_reply_id for rt in found], format="full", concurrency=concurrency
    )
    bodies = {m["id"]: connector._extract_body(m.get("payload") or {}) for m in full}
    kept: list[RepliedThread] = []
    for rt in found:
        text = messages.clean_body(bodies.get(rt.my_reply_id, ""))
        rt.my_reply_text = text
        if rt.tier == TIER_LIKELY and not SIGNOFF.search(text or ""):
            continue
        kept.append(rt)
    kept.sort(key=lambda r: r.my_reply_at)
    return kept


# ── Corpus and stats ────────────────────────────────────────────────────────


def write_corpus(
    rows: list[dict[str, Any]], path: Path, *, title: str = "Pirate Bay — corpus"
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {title}",
        "",
        f"{len(rows)} fil(s) — généré le {date.today().isoformat()}. "
        "Chaque section : le message reçu (extrait), ma réponse, et la suite s'il y en a une.",
        "",
    ]
    for r in sorted(rows, key=lambda x: x.get("my_reply_at", "")):
        head = f"## {r.get('my_reply_at', '')[:10]} · {r.get('sender_name') or r.get('sender')} · {r.get('subject') or '(sans objet)'}"
        lines += [
            head,
            "",
            f"- Tier : `{r.get('tier')}` · domaine `{r.get('sender_domain')}` · "
            f"réponses envoyées {r.get('replies_by_me', 1)} · "
            f"{'**ils ont répondu** le ' + r.get('answer_at', '')[:10] if r.get('answered_back') else 'sans suite'}",
            f"- Gmail : {r.get('url', '')}",
            "",
        ]
        if r.get("original_snippet"):
            lines += ["> " + r["original_snippet"].replace("\n", " "), ""]
        lines += ["```text", (r.get("my_reply_text") or "").strip(), "```", ""]
        if r.get("answer_snippet"):
            lines += [
                "Leur réponse :",
                "",
                "> " + r["answer_snippet"].replace("\n", " "),
                "",
            ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def compute_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_month: Counter[str] = Counter(r.get("month") or "?" for r in rows)
    by_tier: Counter[str] = Counter(r.get("tier") or "?" for r in rows)
    domains: Counter[str] = Counter(r.get("sender_domain") or "?" for r in rows)
    answered = sum(1 for r in rows if r.get("answered_back"))
    return {
        "total": len(rows),
        "answered_back": answered,
        "answer_rate": round(answered / len(rows), 3) if rows else 0.0,
        "by_month": dict(sorted(by_month.items())),
        "by_tier": dict(by_tier),
        "top_domains": domains.most_common(10),
        "drafts": sum(1 for r in rows if r.get("draft_id")),
        "labelled": sum(1 for r in rows if r.get("labels_applied")),
    }


def stats_markdown(st: dict[str, Any], *, ledger_name: str) -> str:
    lines = [
        f"# {ledger_name} — statistiques",
        "",
        f"Généré le {date.today().isoformat()}",
        "",
        f"- Réponses envoyées à des spams : **{st['total']}**",
        f"- Ont répondu en retour : **{st['answered_back']}** ({st['answer_rate'] * 100:.0f} %)",
        f"- Confirmés / probables : {st['by_tier'].get(TIER_CONFIRMED, 0)} / {st['by_tier'].get(TIER_LIKELY, 0)}",
        f"- Étiquetés dans Gmail : {st['labelled']} · brouillons de suite : {st['drafts']}",
        "",
        "## Par mois",
        "",
        "| Mois | Réponses |",
        "|---|---|",
    ]
    for month, n in st["by_month"].items():
        lines.append(f"| {month} | {n} |")
    lines += ["", "## Domaines les plus visés", "", "| Domaine | Fils |", "|---|---|"]
    for dom, n in st["top_domains"]:
        lines.append(f"| {dom} | {n} |")
    lines.append("")
    return "\n".join(lines)


def telegram_text(st: dict[str, Any], *, ledger_name: str) -> str:
    from navig_email._compat import escape_html as esc

    months = " · ".join(f"{m} {n}" for m, n in list(st["by_month"].items())[-6:])
    doms = ", ".join(f"{esc(d)} ({n})" for d, n in st["top_domains"][:5])
    return (
        f"🏴‍☠️ <b>{esc(ledger_name)}</b> — {st['total']} réponse(s) à des spams, "
        f"<b>{st['answered_back']}</b> ont répondu ({st['answer_rate'] * 100:.0f} %)\n"
        f"Par mois : {months or '—'}\nDomaines : {doms or '—'}"
    )
