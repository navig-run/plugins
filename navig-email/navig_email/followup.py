"""``navig email followup --draft`` — the continuing joke, as Gmail *drafts*.

For each ledger thread where the sender answered back and no draft exists yet, this
builds the conversation so far, asks a model for one reply in the space's style
prompt, and saves it as a draft in the thread. It never sends: the operator reads
the draft in Gmail and decides. It is the mailroom's only creative use of a model,
and it goes through ``llm_guard`` like every other one (opt-in, provider printed,
cloud refused unless allowed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import llm_guard, messages

THREAD_CHARS = 9000


@dataclass
class FollowupResult:
    drafted: list[dict[str, Any]] = field(default_factory=list)  # ledger rows updated
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    provider: str = ""


def conversation_text(shaped: list[dict[str, Any]], my_email: str) -> str:
    parts = []
    for m in sorted(shaped, key=lambda x: x.get("internal_ms", 0)):
        who = (
            "MOI (Cybesis Studios)"
            if messages.is_mine(m, my_email)
            else messages.display_name(m["from"]) or m["from"]
        )
        body = messages.clean_body(m.get("body") or m.get("snippet") or "")
        parts.append(f"--- {m['date'][:10]} · {who} · {m['subject']}\n{body}")
    return "\n\n".join(parts)[-THREAD_CHARS:]


async def draft_followups(
    connector,
    rows: list[dict[str, Any]],
    *,
    style_text: str,
    my_email: str,
    only_answered: bool = True,
    model: str | None = None,
    allow_cloud: bool = False,
    limit: int | None = None,
    dry_run: bool = False,
) -> FollowupResult:
    res = FollowupResult()
    resolved = llm_guard.resolve(mode="chat", model=model)
    llm_guard.ensure_allowed(
        resolved, allow_cloud=allow_cloud
    )  # refuse BEFORE reading any thread
    res.provider = str(resolved)

    candidates = [
        r
        for r in rows
        if not r.get("draft_id") and (r.get("answered_back") or not only_answered)
    ]
    if limit:
        candidates = candidates[:limit]

    for row in candidates:
        tid = row.get("thread_id", "")
        try:
            thread = await connector.get_thread(tid, format="full")
            shaped = []
            for m in thread.get("messages") or []:
                body = connector._extract_body(m.get("payload") or {})
                shaped.append(messages.shape(m, body=body))
            if not shaped:
                res.skipped += 1
                continue
            last_theirs = next(
                (
                    m
                    for m in sorted(shaped, key=lambda x: -x.get("internal_ms", 0))
                    if not messages.is_mine(m, my_email)
                ),
                None,
            )
            if last_theirs is None:
                res.skipped += 1
                continue
            convo = conversation_text(shaped, my_email)
            text, _ = llm_guard.generate(
                [
                    {"role": "system", "content": style_text},
                    {
                        "role": "user",
                        "content": "Voici le fil complet. Rédige UNE réponse prête à envoyer au dernier message "
                        "de l'expéditeur, dans la langue du fil, en tenant compte de toute la "
                        "conversation (mémoire du fil).\n\n" + convo,
                    },
                ],
                mode="chat",
                model=model,
                allow_cloud=allow_cloud,
                temperature=0.7,
                max_tokens=700,
            )
            if not text:
                res.errors.append(f"{tid[:10]}: empty reply from {resolved}")
                continue
            subject = last_theirs["subject"] or row.get("subject", "")
            if not subject.lower().startswith("re:"):
                subject = f"Re: {subject}"
            if dry_run:
                row["draft_preview"] = text[:400]
                res.drafted.append(row)
                continue
            draft = await connector.create_draft(
                to=messages.address(last_theirs["from"]) or row.get("sender", ""),
                subject=subject,
                body=text,
                thread_id=tid,
                in_reply_to=last_theirs.get("message_id") or None,
            )
            row["draft_id"] = str(draft.get("id", ""))
            row["draft_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            res.drafted.append(row)
        except llm_guard.CloudRefused:
            raise
        except Exception as exc:  # noqa: BLE001 — one bad thread must not stop the batch
            res.errors.append(f"{tid[:10]}: {exc}")
    return res
