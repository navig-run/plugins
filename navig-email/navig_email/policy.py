"""Auto-reply policies — what the mailroom may answer on its own, for whom, and how often.

A policy is a template (``navig_email/policies/*.yaml``) plus the scope a person gave it when
they enabled it: **which account, which aliases, which senders**. Templates install disabled
and scope-less; ``enable`` refuses without an account, and refuses ``"*"`` senders for a policy
whose template says it needs named ones. Nothing here sends: :func:`evaluate` returns a
decision, and :mod:`navig_email.autoreply` turns it into an outbox job.

Decision order — every step can only make the outcome *more* careful:

1. machine-sent mail, your own mail, spam → ``ignore`` (and loops with other assistants die here)
2. no enabled policy for this account + alias + sender + category → ``ignore``
3. **hard exclusions** (contracts, payments, government, credentials, …) → ``review``
4. sender authentication missing/failed when the policy requires it → ``review``
5. category confidence under the policy's threshold → ``review``
6. per-thread / per-sender / daily limits reached → ``review``
7. otherwise → ``auto`` (send after the policy's delay) or ``draft`` for a drafts-only policy

``review`` means: a reply is drafted and waits for a person. It is never sent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from navig_email import outbox as ob, safety
from navig_email.store import MailroomStore, dumps, get_store

TEMPLATES_DIR = Path(__file__).resolve().parent / "policies"
ACTIONS = ("send", "draft")

# Deterministic category signals (EN / FR / RU). A hit is evidence, not proof: the confidence
# it yields is compared with the policy's threshold.
CATEGORY_WORDS: dict[str, str] = {
    "business_inquiry": r"\b(?:partnership|collaborat\w*|inquir\w*|enquir\w*|proposal|work together|business opportunit\w*|"
                        r"sponsor\w*|quote|pricing|rates?|partenariat|collaboration|proposition|demande d'information|"
                        r"devis|tarifs?|сотрудничеств\w*|предложени\w*|партн[её]рств\w*|расценк\w*)\b",
    "support": r"\b(?:support|help|issue|problem|bug|error|crash\w*|not working|doesn't work|broken|aide|problème|"
               r"erreur|ne fonctionne pas|bloqué|помощ\w*|проблем\w*|ошибк\w*|не работает|сломал\w*)\b",
    "confirmation": r"\b(?:did you (?:receive|get)|just checking|confirm(?:ing)? (?:receipt|you received)|have you received|"
                    r"bien reçu|avez-vous reçu|pouvez-vous confirmer|получили ли|подтвердите получение)\b",
}
_CAT_RE = {k: re.compile(v, re.IGNORECASE) for k, v in CATEGORY_WORDS.items()}
#: Alias local-parts that are themselves evidence of a category.
ALIAS_HINTS = {"support": "support", "help": "support", "contact": "business_inquiry", "hello": "business_inquiry",
               "business": "business_inquiry", "partners": "business_inquiry", "sales": "business_inquiry"}

_DUR = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)


class PolicyError(ValueError):
    """A policy that cannot be enabled as asked."""


def _dur(text: Any, default: timedelta) -> timedelta:
    try:
        return ob.parse_delay(str(text)) if str(text).strip() not in ("", "0", "0m") else timedelta(0)
    except ob.OutboxError:
        return default


@dataclass
class Policy:
    id: str
    title: str = ""
    action: str = "send"
    category: str = "any"
    senders: list[str] = field(default_factory=list)
    require_auth: bool = True
    confidence: float = 0.8
    delay: timedelta = ob.DEFAULT_DELAY
    per_thread: int = 1
    sender_cooldown: timedelta = timedelta(hours=24)
    daily: int = 20
    style: str = "plain"
    identity: str = ""
    model: str = ""
    say: str = ""
    facts: list[str] = field(default_factory=list)
    required_details: list[str] = field(default_factory=list)
    review_words: list[str] = field(default_factory=list)
    notify: bool = True
    # scope, set when enabled
    enabled: bool = False
    account: str = ""
    aliases: list[str] = field(default_factory=list)
    version: int = 1

    @classmethod
    def from_spec(cls, spec: dict[str, Any], *, enabled: bool = False, version: int = 1) -> Policy:
        limits = spec.get("limits") or {}
        return cls(
            id=str(spec["id"]),
            title=str(spec.get("title") or spec["id"]),
            action=str(spec.get("action") or "send"),
            category=str(spec.get("category") or "any"),
            senders=[str(s).strip().lower() for s in spec.get("senders") or []],
            require_auth=bool(spec.get("require_auth", True)),
            confidence=float(spec.get("confidence", 0.8)),
            delay=_dur(spec.get("delay", "15m"), ob.DEFAULT_DELAY),
            per_thread=int(limits.get("per_thread", 1)),
            sender_cooldown=_dur(limits.get("sender_cooldown", "24h"), timedelta(hours=24)),
            daily=int(limits.get("daily", 20)),
            style=str(spec.get("style") or "plain"),
            identity=str(spec.get("identity") or ""),
            model=str(spec.get("model") or ""),
            say=str(spec.get("say") or ""),
            facts=[str(f) for f in spec.get("facts") or []],
            required_details=[str(d) for d in spec.get("required_details") or []],
            review_words=[str(w) for w in spec.get("review_words") or []],
            notify=bool(spec.get("notify", True)),
            enabled=enabled,
            account=str(spec.get("account") or "").lower(),
            aliases=[str(a).strip().lower() for a in spec.get("aliases") or []],
            version=version,
        )

    def spec(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "action": self.action, "category": self.category,
            "senders": self.senders, "require_auth": self.require_auth, "confidence": self.confidence,
            "delay": f"{int(self.delay.total_seconds() // 60)}m",
            "limits": {"per_thread": self.per_thread,
                       "sender_cooldown": f"{int(self.sender_cooldown.total_seconds() // 60)}m",
                       "daily": self.daily},
            "style": self.style, "identity": self.identity, "model": self.model, "say": self.say,
            "facts": self.facts, "required_details": self.required_details,
            "review_words": self.review_words, "notify": self.notify,
            "account": self.account, "aliases": self.aliases,
        }

    def instruction(self, msg: dict[str, Any]) -> str:
        """What the generated reply must (and must only) say."""
        parts = [self.say.strip()] if self.say.strip() else []
        if self.facts:
            parts.append("Facts you may state (and no others):\n" + "\n".join(f"- {f}" for f in self.facts))
        if self.required_details:
            text = " ".join(str(msg.get(k) or "") for k in ("subject", "body")).lower()
            missing = [d for d in self.required_details if d.lower() not in text]
            if missing:
                parts.append("Ask for exactly these missing details:\n" + "\n".join(f"- {d}" for d in missing))
        parts.append("This is an automatic reply: keep it short, make no promise, and never claim that a "
                     "person has read or handled the message.")
        return "\n\n".join(parts)


# ── templates & the store ──────────────────────────────────────────────────


def load_templates() -> dict[str, dict[str, Any]]:
    import yaml

    out: dict[str, dict[str, Any]] = {}
    for f in sorted(TEMPLATES_DIR.glob("*.yaml")):
        spec = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        if isinstance(spec, dict) and spec.get("id"):
            out[str(spec["id"])] = spec
    return out


def _s(store: MailroomStore | None) -> MailroomStore:
    return store or get_store()


def install_templates(store: MailroomStore | None = None) -> int:
    """Insert every shipped template that is not in the store yet — disabled. Idempotent;
    never touches a policy a person already configured."""
    s = _s(store)
    added = 0
    for pid, spec in load_templates().items():
        cur = s._write(
            "INSERT OR IGNORE INTO policies (policy_id, template, enabled, spec_json, updated_at) VALUES (?, ?, 0, ?, ?)",
            (pid, pid, dumps(spec), ob.iso(ob.now_utc())),
        )
        added += cur.rowcount
    return added


def get(policy_id: str, store: MailroomStore | None = None) -> Policy | None:
    import json

    row = _s(store)._read_one("SELECT * FROM policies WHERE policy_id = ?", (policy_id,))
    if row is None:
        return None
    return Policy.from_spec(json.loads(row["spec_json"]), enabled=bool(row["enabled"]), version=int(row["version"]))


def all_policies(store: MailroomStore | None = None) -> list[Policy]:
    s = _s(store)
    install_templates(s)
    return [p for p in (get(r["policy_id"], s) for r in s._read_all("SELECT policy_id FROM policies ORDER BY policy_id")) if p]


def enabled_for(account: str, store: MailroomStore | None = None) -> list[Policy]:
    acct = (account or "").strip().lower()
    return [p for p in all_policies(store) if p.enabled and p.account == acct]


def enable(
    policy_id: str,
    *,
    account: str,
    aliases: list[str] | None = None,
    senders: list[str] | None = None,
    facts: list[str] | None = None,
    delay: str | None = None,
    actor: str = "user",
    store: MailroomStore | None = None,
) -> Policy:
    """Turn a policy on for one account (and optionally only some aliases / senders)."""
    s = _s(store)
    install_templates(s)
    pol = get(policy_id, s)
    if pol is None:
        raise PolicyError(f"no policy {policy_id!r} — see `policy list`")
    acct = (account or "").strip().lower()
    if not acct or "@" not in acct:
        raise PolicyError("a policy is enabled for one account: pass --account you@gmail.com")
    pol.account = acct
    if aliases is not None:
        pol.aliases = [a.strip().lower() for a in aliases if a.strip()]
    if senders is not None:
        pol.senders = [x.strip().lower() for x in senders if x.strip()]
    if facts is not None:
        pol.facts = [f for f in facts if f.strip()]
    if delay is not None:
        pol.delay = ob.parse_delay(delay)
    if pol.id == "trusted-repetitive" or load_templates().get(pol.id, {}).get("senders") == []:
        if not pol.senders or "*" in pol.senders:
            raise PolicyError(f"{pol.id} answers named senders only: pass --sender (address or domain)")
    if pol.action not in ACTIONS:
        raise PolicyError(f"unknown action {pol.action!r}")
    with s.transaction() as conn:
        conn.execute(
            "UPDATE policies SET enabled = 1, spec_json = ?, enabled_by = ?, enabled_at = ?, "
            "version = version + 1, updated_at = ? WHERE policy_id = ?",
            (dumps(pol.spec()), actor, ob.iso(ob.now_utc()), ob.iso(ob.now_utc()), pol.id),
        )
        s.audit("policy.enable", actor=actor, account=acct, ref=pol.id,
                summary=f"{pol.action} · aliases={','.join(pol.aliases) or 'all'} · senders={','.join(pol.senders) or '-'}",
                conn=conn)
    return get(pol.id, s)  # type: ignore[return-value]


def disable(policy_id: str, *, actor: str = "user", store: MailroomStore | None = None) -> Policy:
    """Off. Queued replies from this policy stop at dispatch (the revalidator below)."""
    s = _s(store)
    if get(policy_id, s) is None:
        raise PolicyError(f"no policy {policy_id!r}")
    with s.transaction() as conn:
        conn.execute("UPDATE policies SET enabled = 0, version = version + 1, updated_at = ? WHERE policy_id = ?",
                     (ob.iso(ob.now_utc()), policy_id))
        s.audit("policy.disable", actor=actor, ref=policy_id, conn=conn)
    return get(policy_id, s)  # type: ignore[return-value]


def still_authorised(job: dict[str, Any]) -> str:
    """Outbox revalidator: a policy reply is only sent while its policy is on, for that account."""
    if job.get("origin") != "policy":
        return ""
    pol = get(job.get("policy_id") or "")
    if pol is None:
        return f"policy {job.get('policy_id')!r} no longer exists"
    if not pol.enabled:
        return f"policy {pol.id} was disabled"
    if pol.action != "send":
        return f"policy {pol.id} only drafts"
    if pol.account != (job.get("account") or "").lower():
        return f"policy {pol.id} is not enabled for {job.get('account')}"
    if pol.aliases and (job.get("from_addr") or "").split("@")[0] not in [a.split("@")[0] for a in pol.aliases]:
        return f"policy {pol.id} does not cover {job.get('from_addr')}"
    return ""


# ── evaluation ─────────────────────────────────────────────────────────────


@dataclass
class Decision:
    outcome: str  # ignore | review | auto | draft
    policy_id: str = ""
    category: str = ""
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)

    def note(self) -> str:
        return "; ".join(self.reasons)


def classify(msg: dict[str, Any]) -> tuple[str, float]:
    """``(category, confidence)`` from words and the alias. ``("", 0)`` when nothing points anywhere."""
    text = " ".join(str(msg.get(k) or "") for k in ("subject", "body", "snippet"))
    alias = str(msg.get("alias") or "").split("@")[0]
    scores: dict[str, float] = {}
    for cat, rx in _CAT_RE.items():
        hits = len(set(m.group(0).lower() for m in rx.finditer(text)))
        if hits:
            scores[cat] = min(0.95, 0.5 + 0.15 * hits)
    if alias in ALIAS_HINTS:
        cat = ALIAS_HINTS[alias]
        scores[cat] = min(0.95, scores.get(cat, 0.45) + 0.25)
    if not scores:
        return "", 0.0
    best = max(scores, key=scores.get)
    ranked = sorted(scores.values(), reverse=True)
    conf = scores[best] - (0.2 if len(ranked) > 1 and ranked[0] - ranked[1] < 0.1 else 0.0)  # ambiguous
    return best, round(max(conf, 0.0), 2)


def _sender_allowed(pol: Policy, sender: str) -> bool:
    if "*" in pol.senders:
        return True
    dom = sender.rpartition("@")[2]
    return any(s == sender or s == dom or s == "@" + dom or dom.endswith("." + s.lstrip("@")) for s in pol.senders)


def _alias_covered(pol: Policy, msg: dict[str, Any]) -> bool:
    if not pol.aliases:
        return True
    alias = str(msg.get("alias") or "").split("@")[0]
    to = " ".join(str(msg.get(k) or "") for k in ("to", "cc")).lower()
    return any(a.split("@")[0] == alias or a in to for a in pol.aliases)


def _limits(pol: Policy, msg: dict[str, Any], sender: str, now: datetime, store: MailroomStore) -> str:
    live = "state NOT IN ('CANCELLED')"
    n_thread = store._read_one(
        f"SELECT COUNT(*) FROM outbox WHERE origin='policy' AND account=? AND thread_id=? AND {live}",
        (pol.account, msg.get("thread_id") or ""),
    )[0]
    if n_thread >= pol.per_thread:
        return f"already {n_thread} automatic repl{'y' if n_thread == 1 else 'ies'} in this thread"
    if pol.sender_cooldown > timedelta(0):
        since = ob.iso(now - pol.sender_cooldown)
        recent = store._read_one(
            f"SELECT COUNT(*) FROM outbox WHERE origin='policy' AND account=? AND to_addrs LIKE ? AND created_at >= ? AND {live}",
            (pol.account, f"%{sender}%", since),
        )[0]
        if recent:
            return f"{sender} got an automatic reply in the last {pol.sender_cooldown}"
    day = store._read_one(
        f"SELECT COUNT(*) FROM outbox WHERE origin='policy' AND policy_id=? AND created_at >= ? AND {live}",
        (pol.id, ob.iso(now - timedelta(days=1))),
    )[0]
    if day >= pol.daily:
        return f"policy {pol.id} reached its daily limit ({pol.daily})"
    return ""


def evaluate(
    msg: dict[str, Any],
    *,
    account: str,
    my_addresses: list[str] | None = None,
    policies: list[Policy] | None = None,
    now: datetime | None = None,
    store: MailroomStore | None = None,
) -> Decision:
    """What to do with one incoming message. Pure apart from reading the outbox counts."""
    s = _s(store)
    t = now or ob.now_utc()
    sender = parseaddr(msg.get("from") or "")[1].lower()
    mine = {a.lower() for a in (my_addresses or [])} | {account.lower()}
    labels = set(msg.get("labels") or [])

    if "SENT" in labels or sender in mine:
        return Decision("ignore", reasons=["your own message"])
    if "INBOX" not in labels:
        return Decision("ignore", reasons=["not in the inbox"])
    machine = safety.automated(msg)
    if machine:
        return Decision("ignore", reasons=[f"automated mail ({machine})"])
    if not sender:
        return Decision("ignore", reasons=["no sender address"])

    pols = policies if policies is not None else enabled_for(account, s)
    category, conf = classify(msg)
    candidates = [p for p in pols if p.enabled and p.account == account.lower()
                  and _alias_covered(p, msg) and _sender_allowed(p, sender)
                  and (p.category in ("any", category))]
    if not candidates:
        return Decision("ignore", category=category, confidence=conf, reasons=["no enabled policy covers this message"])
    # The most specific policy wins: a category match over "any", named senders over "*".
    pol = sorted(candidates, key=lambda p: (p.category == "any", "*" in p.senders, p.id))[0]
    # A category policy is as sure as the classification. An "any" policy over NAMED senders is
    # certain about what matters (who wrote); over "*" it is only as sure as the classification.
    named = bool(pol.senders) and "*" not in pol.senders
    confidence = 1.0 if (pol.category == "any" and named) else conf
    d = Decision("auto" if pol.action == "send" else "draft", policy_id=pol.id,
                 category=category or pol.category, confidence=confidence)

    if pol.action == "draft":
        return d  # drafting is always safe: nothing leaves without a person

    excl = safety.hard_exclusions(msg, extra=pol.review_words)
    if excl:
        d.outcome, d.reasons = "review", [f"needs a person: {excl.summary()}"]
        return d
    auth = safety.authentication(msg)
    if pol.require_auth and auth != "pass":
        d.outcome, d.reasons = "review", [f"sender not authenticated (DKIM/SPF/DMARC: {auth})"]
        return d
    if d.confidence < pol.confidence:
        d.outcome, d.reasons = "review", [f"category {d.category or '?'} at {d.confidence:.2f} < {pol.confidence:.2f}"]
        return d
    limit = _limits(pol, msg, sender, t, s)
    if limit:
        d.outcome, d.reasons = "review", [limit]
        return d
    d.reasons = [f"policy {pol.id}: {d.category} ({d.confidence:.2f}), sender authenticated"]
    return d
