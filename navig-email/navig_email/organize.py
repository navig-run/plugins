"""Gmail labels and filters: audit what is there, propose a tidier set, migrate without losing anything.

Three steps, each one explicit:

1. **audit** (read-only) — every label with its message count; equivalent labels spelled
   differently; unused labels; filters that conflict, repeat each other, point at labels that do
   not exist, or forward mail somewhere.
2. **preview** — a proposed taxonomy built from *this* mailbox: an existing label maps to a
   target only when its name or the mail in it says so (identities from the space, sender
   patterns), and a target is proposed only when at least one message would land in it.
3. **apply** — *additive*: new labels are created and added to the messages; the old labels and
   filters stay where they are. A snapshot (labels, filters, every message touched) is written
   first, so ``rollback`` can undo it. Removing the old labels / filters is a separate
   ``retire`` step a person runs after checking the result.

Gmail system labels (INBOX, SENT, CATEGORY_*, …) are never touched. Gmail has no filter update:
a changed filter is a new filter plus, at ``retire``, the deletion of the old one.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SYSTEM_PREFIXES = ("CATEGORY_",)
SYSTEM_IDS = {"INBOX", "SENT", "DRAFT", "SPAM", "TRASH", "UNREAD", "STARRED", "IMPORTANT", "CHAT"}
MAILROOM_LABELS = {"Mailroom/Digested", "Mailroom/Needs Action"}

#: Target label → (keywords in an existing label's name, sender-domain hints). A target is only
#: proposed when the mailbox has mail for it.
TAXONOMY: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "NAVIG/GitHub": (("github", "pull request", "gh"), ("github.com",)),
    "NAVIG/Development": (("navig", "dev", "build", "ci"), ()),
    "Opportunities/Jobs": (("job", "jobs", "emploi", "mission", "freelance", "recrutement"),
                           ("free-work.com", "linkedin.com", "welcometothejungle.com", "indeed.com", "malt.fr")),
    "Opportunities/Events": (("event", "events", "evenement", "meetup", "conference", "webinar"),
                             ("eventbrite.com", "meetup.com", "lu.ma")),
    "Newsletters/Products": (("newsletter", "producthunt", "products", "digest"), ("producthunt.com",)),
    "Research/Technology": (("research", "tech", "veille"), ()),
    "Personal/Administration": (("admin", "impots", "impot", "banque", "sante", "logement", "identite", "caf"),
                                ("impots.gouv.fr", "ameli.fr", "caf.fr")),
}


def _norm(name: str) -> str:
    t = unicodedata.normalize("NFKD", name or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = re.sub(r"[\s_\-./]+", " ", t).strip()
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in t.split())


def is_system(label: dict[str, Any]) -> bool:
    lid = str(label.get("id") or "")
    return label.get("type") == "system" or lid in SYSTEM_IDS or lid.startswith(SYSTEM_PREFIXES)


@dataclass
class LabelInfo:
    id: str
    name: str
    messages: int = 0
    system: bool = False


@dataclass
class Audit:
    labels: list[LabelInfo] = field(default_factory=list)
    equivalent: list[list[str]] = field(default_factory=list)  # groups of label names
    unused: list[str] = field(default_factory=list)
    filters: list[dict[str, Any]] = field(default_factory=list)
    filters_access: str = "ok"  # ok | needs_scope | error: …
    conflicts: list[str] = field(default_factory=list)
    redundant: list[str] = field(default_factory=list)
    forwarding: list[str] = field(default_factory=list)
    dangling: list[str] = field(default_factory=list)
    top_senders: list[tuple[str, int]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _crit_key(criteria: dict[str, Any]) -> str:
    return json.dumps({k: v for k, v in sorted((criteria or {}).items())}, sort_keys=True)


async def audit(connector, *, sample: int = 300) -> Audit:
    out = Audit()
    for lbl in await connector.list_labels():
        sysl = is_system(lbl)
        count = 0
        if not sysl:
            try:
                full = await connector.get_label(lbl["id"])
                count = int(full.get("messagesTotal") or 0)
            except Exception:  # noqa: BLE001 — a count we cannot read is reported as 0 with the label kept
                count = 0
        out.labels.append(LabelInfo(id=str(lbl["id"]), name=str(lbl.get("name") or lbl["id"]), messages=count, system=sysl))
    user = [l for l in out.labels if not l.system]
    groups: dict[str, list[str]] = {}
    for l in user:
        groups.setdefault(_norm(l.name), []).append(l.name)
    out.equivalent = [sorted(v) for v in groups.values() if len(v) > 1]
    out.unused = sorted(l.name for l in user if l.messages == 0 and l.name not in MAILROOM_LABELS)

    list_filters = getattr(connector, "list_filters", None)
    if not callable(list_filters):
        out.filters_access = "needs_scope"
    else:
        try:
            out.filters = await list_filters()
        except Exception as exc:  # noqa: BLE001
            status = getattr(exc, "status_code", None)
            out.filters_access = "needs_scope" if status in (401, 403) else f"error: {str(exc)[:120]}"
    ids = {l.id for l in out.labels}
    by_crit: dict[str, list[dict[str, Any]]] = {}
    for f in out.filters:
        by_crit.setdefault(_crit_key(f.get("criteria") or {}), []).append(f)
        act = f.get("action") or {}
        if act.get("forward"):
            out.forwarding.append(f"filter {f.get('id')} forwards matching mail to {act['forward']}")
        for lid in (act.get("addLabelIds") or []):
            if lid not in ids:
                out.dangling.append(f"filter {f.get('id')} adds a label that no longer exists ({lid})")
    for crit, fs in by_crit.items():
        if len(fs) < 2:
            continue
        actions = {json.dumps(f.get("action") or {}, sort_keys=True) for f in fs}
        names = ", ".join(str(f.get("id")) for f in fs)
        (out.redundant if len(actions) == 1 else out.conflicts).append(f"{names} share criteria {crit}")

    counts: dict[str, int] = {}
    entries = await connector.iter_message_ids("newer_than:180d -in:sent", limit=sample)
    meta = await connector.get_many([e["id"] for e in entries], format="metadata")
    from navig_email.messages import domain_of

    for m in meta:
        hdr = {h["name"].lower(): h["value"] for h in (m.get("payload") or {}).get("headers") or []}
        d = domain_of(hdr.get("from", ""))
        if d:
            counts[d] = counts.get(d, 0) + 1
    out.top_senders = sorted(counts.items(), key=lambda kv: -kv[1])[:20]
    return out


# ── preview ────────────────────────────────────────────────────────────────


@dataclass
class Move:
    source: str  # an existing label name, or "from:<domain>"
    target: str
    messages: int
    why: str


@dataclass
class Plan:
    moves: list[Move] = field(default_factory=list)
    merges: list[dict[str, Any]] = field(default_factory=list)  # equivalent labels → one name
    new_labels: list[str] = field(default_factory=list)
    filters: list[dict[str, Any]] = field(default_factory=list)  # {criteria, action:{addLabel: name}}
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _identity_targets(paths: Any) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    """``Cybesis/Business`` etc. from the space's identities — the business's own name, not a guess."""
    out: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    if paths is None:
        return out
    try:
        from navig_email import voice

        idents = voice.identities(paths)
    except Exception:  # noqa: BLE001
        return out
    for ident in idents.values():
        company = (ident.company or "").split()[0] if ident.company else ""
        if not company or ident.name == "personal":
            continue
        root = company.strip(",.").title()
        domains = tuple(ident.domains)
        out[f"{root}/Business"] = ((root.lower(), "business", "contact", "support"), domains)
        out[f"{root}/Finance"] = (("facture", "invoice", "finance", "compta", "paiement"), ())
    return out


def preview(a: Audit, *, paths: Any = None) -> Plan:
    plan = Plan()
    targets = {**_identity_targets(paths), **TAXONOMY}
    existing = {l.name for l in a.labels}
    for l in a.labels:
        if l.system or l.name in MAILROOM_LABELS or l.messages == 0:
            continue
        n = _norm(l.name)
        for target, (words, _domains) in targets.items():
            if l.name == target or n == _norm(target):
                break
            if any(re.search(rf"\b{re.escape(_norm(w))}\b", n) for w in words):
                plan.moves.append(Move(l.name, target, l.messages, f"the label name says {target.split('/')[-1].lower()}"))
                break
    for domain, n in a.top_senders:
        for target, (_words, domains) in targets.items():
            if n >= 3 and any(domain == d or domain.endswith("." + d) for d in domains):
                plan.moves.append(Move(f"from:{domain}", target, n, f"{n} recent messages from {domain}"))
                plan.filters.append({"criteria": {"from": domain}, "action": {"addLabel": target}})
                break
    for group in a.equivalent:
        keep = max(group, key=lambda name: next((l.messages for l in a.labels if l.name == name), 0))
        plan.merges.append({"labels": group, "into": keep})
    used_targets = sorted({m.target for m in plan.moves} | {m["into"] for m in plan.merges})
    plan.new_labels = [t for t in used_targets if t not in existing]
    if a.filters_access == "needs_scope":
        plan.notes.append("Filters were not read: the Gmail connection lacks filter permission. Set "
                          "`navig config set connectors.gmail.filters true`, then reconnect Gmail, to include them.")
    if a.forwarding:
        plan.notes.append("Forwarding filters exist — review them; this tool never changes forwarding.")
    return plan


# ── apply / retire / rollback ──────────────────────────────────────────────


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


async def apply(connector, plan: Plan, a: Audit, *, snapshot_dir: Path, limit_per_move: int = 2000) -> dict[str, Any]:
    """Additive migration: create targets, add them to the messages, create new filters. Nothing removed."""
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snap: dict[str, Any] = {"at": _stamp(), "labels": [asdict(l) for l in a.labels], "filters": a.filters,
                            "added": {}, "created_filters": [], "plan": plan.as_dict()}
    path = snapshot_dir / f"organize-snapshot-{snap['at']}.json"
    path.write_text(json.dumps(snap, indent=1), encoding="utf-8")  # written BEFORE any change
    name_to_id = {l.name: l.id for l in a.labels}
    done: dict[str, int] = {}
    for mv in plan.moves + [Move(src, m["into"], 0, "merge") for m in plan.merges for src in m["labels"] if src != m["into"]]:
        target_id = name_to_id.get(mv.target) or await connector.ensure_label(mv.target)
        name_to_id[mv.target] = target_id
        if mv.source.startswith("from:"):
            found = await connector.iter_message_ids(f"from:{mv.source[5:]}", limit=limit_per_move)
        else:  # by label id: a name with spaces does not survive Gmail's label: search syntax
            found = await connector.iter_message_ids("", limit=limit_per_move, label_ids=[name_to_id[mv.source]])
        ids = [e["id"] for e in found]
        for mid in ids:
            await connector.modify_message(mid, add=[target_id])
        snap["added"].setdefault(target_id, []).extend(ids)
        done[f"{mv.source} → {mv.target}"] = len(ids)
        path.write_text(json.dumps(snap, indent=1), encoding="utf-8")
    create = getattr(connector, "create_filter", None)
    if a.filters_access == "ok" and callable(create):
        for f in plan.filters:
            lid = name_to_id.get(f["action"]["addLabel"]) or await connector.ensure_label(f["action"]["addLabel"])
            made = await create(f["criteria"], {"addLabelIds": [lid]})
            snap["created_filters"].append(made.get("id"))
        path.write_text(json.dumps(snap, indent=1), encoding="utf-8")
    return {"snapshot": str(path), "labelled": done, "filters_created": len(snap["created_filters"])}


async def rollback(connector, snapshot: Path) -> dict[str, int]:
    """Undo an ``apply``: remove the labels it added, delete the filters it created."""
    snap = json.loads(snapshot.read_text(encoding="utf-8"))
    removed = 0
    for lid, ids in (snap.get("added") or {}).items():
        for mid in ids:
            await connector.modify_message(mid, remove=[lid])
            removed += 1
    deleted = 0
    delete = getattr(connector, "delete_filter", None)
    for fid in snap.get("created_filters") or []:
        if fid and callable(delete):
            await delete(fid)
            deleted += 1
    return {"labels_removed": removed, "filters_deleted": deleted}


async def retire(connector, plan: Plan, a: Audit, *, labels: bool = True) -> dict[str, int]:
    """After a person checked the result: take the OLD labels off the migrated messages.

    Labels themselves are not deleted (an empty label costs nothing and may be referenced
    elsewhere); old filters are not deleted here either — list them in the audit and remove
    them by hand or with a later explicit step.
    """
    by_name = {l.name: l for l in a.labels}
    taken = 0
    for mv in plan.moves:
        if mv.source.startswith("from:") or not labels:
            continue
        old = by_name.get(mv.source)
        if old is None or old.system:
            continue
        for e in await connector.iter_message_ids("", limit=5000, label_ids=[old.id]):
            await connector.modify_message(e["id"], remove=[old.id])
            taken += 1
    return {"labels_taken_off": taken}
