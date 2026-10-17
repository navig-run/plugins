"""Incoming mail → policy decision → outbox job. The only producer of ``origin='policy'`` jobs.

For each new message the watch hands over:

* ``ignore`` — nothing happens (machines, lists, your own mail, no policy).
* ``draft`` — a reply is written and left as a DRAFT job. Never sent.
* ``review`` — a reply is written and left awaiting approval, with the reason attached.
* ``auto`` — a reply is written; only if it passes the quality gate with **no issue at all**
  (``auto_ok``) is it approved on the policy's behalf and scheduled after the policy's delay
  (default 15 minutes — cancellable from the OS, Telegram or ``outbox cancel``). Anything
  less becomes ``review``.

A model failure — refused cloud provider, empty output, an exception — never becomes a send:
the message simply gets no automatic reply and the error is reported.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from navig_email import compose, outbox as ob, policy as P, senders, voice

logger = logging.getLogger("navig_email")


@dataclass
class AutoResult:
    message_id: str
    decision: P.Decision
    job_id: str = ""
    job_state: str = ""
    error: str = ""


@dataclass
class AutoRun:
    results: list[AutoResult] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.results:
            key = r.job_state.lower() if r.job_state else r.decision.outcome
            out[key] = out.get(key, 0) + 1
        return out


async def process(
    connector: Any,
    msgs: list[dict[str, Any]],
    *,
    paths: Any,
    account: str,
    model: str | None = None,
    allow_cloud: bool = False,
    fallback_model: str | None = None,
    dry_run: bool = False,
    store=None,
) -> AutoRun:
    run = AutoRun()
    pols = P.enabled_for(account, store)
    if not pols:
        return run
    mine = senders.authorized_senders(paths, account)
    for msg in msgs:
        decision = P.evaluate(msg, account=account, my_addresses=mine, policies=pols, store=store)
        res = AutoResult(message_id=str(msg.get("id") or ""), decision=decision)
        run.results.append(res)
        if decision.outcome == "ignore" or dry_run:
            continue
        pol = next(p for p in pols if p.id == decision.policy_id)
        try:
            await _act(connector, msg, pol, decision, res, paths=paths, account=account, model=model,
                       allow_cloud=allow_cloud, fallback_model=fallback_model, store=store)
        except ob.OutboxError as exc:
            res.error = str(exc)
        except Exception as exc:  # noqa: BLE001 — one message's failure never stops the pass or sends
            logger.warning("auto-reply for %s failed", res.message_id, exc_info=True)
            res.error = f"{type(exc).__name__}: {exc}"
    return run


async def _act(connector, msg, pol: P.Policy, decision: P.Decision, res: AutoResult, *, paths, account, model,
               allow_cloud, fallback_model, store) -> None:
    mid = str(msg.get("id") or "")
    done = ob._store(store)._read_one(
        "SELECT job_id, state FROM outbox WHERE origin='policy' AND account=? AND reply_to_msg=? AND policy_id=?",
        (account.lower(), mid, pol.id),
    )
    if done is not None:  # this message was already answered (or is waiting) — no second model call
        res.job_id, res.job_state = done["job_id"], done["state"]
        return
    out = await compose.draft_reply(
        connector,
        str(msg.get("thread_id") or ""),
        paths=paths,
        my_email=account,
        style=pol.style or "plain",
        identity_name=pol.identity,
        say=pol.instruction(msg),
        model=pol.model or model,
        allow_cloud=allow_cloud,
        account=account,
        save=False,
        fallback_model=fallback_model,
    )
    if out.errors or not out.text:
        res.error = "; ".join(out.errors) or "no reply generated"
        return
    ident = voice.identities(paths).get(out.identity) if out.identity else None
    sender = senders.pick_sender(paths, account, alias_hint=out.alias, to_header=out.to_header)
    subject = out.subject if out.subject.lower().startswith("re:") else f"Re: {out.subject}"
    job = ob.create(
        account=account, to=out.to_addr, subject=subject, body=out.text, from_addr=sender,
        from_name=ident.display if ident else "", identity=out.identity, thread_id=out.thread_id,
        reply_to_msg=mid, in_reply_to=out.in_reply_to,
        references=out.references, origin="policy", policy_id=pol.id, style=out.style, model=out.provider,
        quality=out.quality, paths=paths, actor=f"policy:{pol.id}", store=store,
    )
    if job["state"] != ob.DRAFT:  # already handled on an earlier pass (op_key)
        res.job_id, res.job_state = job["job_id"], job["state"]
        return
    if decision.outcome == "draft":
        res.job_id, res.job_state = job["job_id"], job["state"]
        return
    auto_ok = bool((out.quality or {}).get("auto_ok"))
    if decision.outcome == "auto" and not auto_ok:
        issues = "; ".join(i["message"] for i in (out.quality or {}).get("issues") or [])
        decision.outcome = "review"
        decision.reasons.append(f"the reply needs a look: {issues}")
    job = ob.submit(job["job_id"], actor=f"policy:{pol.id}", store=store)
    if decision.outcome == "auto":
        job = ob.approve(job["job_id"], content_hash_seen=job["content_hash"],
                         send_at=ob.now_utc() + pol.delay, actor=f"policy:{pol.id}", store=store)
    else:
        ob._store(store).audit("policy.review", actor=f"policy:{pol.id}", account=account, ref=job["job_id"],
                               summary=decision.note()[:300])
    res.job_id, res.job_state = job["job_id"], job["state"]
