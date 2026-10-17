"""What a conversation decided, left open, and produced — deterministically, with citations.

No model is called: everything here is read from what was said and from what the person
marked. Each item cites the message it came from (``message_id``), so a surface can open the
exact message, and says on what authority it stands (``basis``):

* ``marked``    — the person bookmarked it as a decision / todo / snippet in the library
* ``user``      — the person wrote it, with an explicit marker ("Decision: …", "TODO: …")
* ``assistant`` — the assistant proposed it; never reported as a decision someone took

Decisions about the same subject supersede each other in time order: the newest is
``current``, the older ones ``superseded`` (with ``superseded_by``). A subject is explicit —
``Decision (database): SQLite`` or a bookmark note ``subject: database`` — otherwise only an
identical decision counts as the same one. Anything subtler needs a model, and is left to
NAVIG's knowledge extraction, not guessed here.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

_MARKERS_DECISION = r"(?:final\s+decision|decision|decided|we\s+decided|décision|décidé|решение|решили)"
_MARKERS_TODO = r"(?:todo|to\s+do|to-do|action\s+item|next\s+step|à\s+faire|сделать|задача)"
_MARKERS_QUESTION = r"(?:open\s+question|question\s+ouverte|открытый\s+вопрос)"
_SEP = r"\s*[:\-–—]\s*"
_SUBJECT = r"(?:\s*\(([^)\n]{1,80})\))?"

DECISION_RE = re.compile(rf"^\s*(?:[-*>]\s*)?\**{_MARKERS_DECISION}\**{_SUBJECT}{_SEP}(.+?)\s*$", re.I | re.M)
TODO_RE = re.compile(rf"^\s*(?:[-*>]\s*)?\**{_MARKERS_TODO}\**{_SEP}(.+?)\s*$", re.I | re.M)
QUESTION_RE = re.compile(rf"^\s*(?:[-*>]\s*)?\**{_MARKERS_QUESTION}\**{_SEP}(.+?)\s*$", re.I | re.M)
CHECKBOX_RE = re.compile(r"^\s*[-*]\s*\[( |x|X)\]\s+(.+?)\s*$", re.M)
FENCE_RE = re.compile(r"```([\w+#.-]*)\n(.*?)(?:\n```|\Z)", re.S)
URL_RE = re.compile(r"https?://[^\s<>()\"'`\]]+[^\s<>()\"'`\].,;:!?]")
_SUBJECT_NOTE = re.compile(r"^\s*subject\s*[:=]\s*(.+?)\s*$", re.I | re.M)
_KEY = re.compile(r"[^\w]+", re.U)


def subject_key(text: str) -> str:
    return _KEY.sub(" ", (text or "").casefold()).strip()[:120]


@dataclass
class Item:
    kind: str  # decision · task · question · snippet · link · highlight
    text: str
    conversation_id: str
    message_id: str | None
    role: str = ""
    ts: str | None = None
    basis: str = "user"  # marked · user · assistant
    subject: str = ""
    status: str = ""  # decision: current|superseded|restated|proposed · task: open|done · question: open
    superseded_by: str | None = None
    lang: str = ""
    note: str = ""
    position: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        return f"{self.conversation_id}#{self.message_id}#{self.kind}#{subject_key(self.text)[:60]}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clip(text: str, limit: int = 600) -> str:
    text = " ".join(text.split()) if "\n" not in text[:limit] else text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def extract(conv: dict[str, Any], messages: list[dict[str, Any]], bookmarks: list[dict[str, Any]] | None = None) -> list[Item]:
    """Every item in one conversation (active branch messages + the person's bookmarks)."""
    cid = conv["id"]
    items: list[Item] = []
    by_id = {m["id"]: m for m in messages}
    for m in messages:
        role, text, mid, ts, pos = m["role"], m.get("text") or "", m["id"], m.get("created_at"), m.get("position", 0.0)
        basis = "user" if role == "user" else "assistant"
        prose = FENCE_RE.sub("", text)
        for mt in DECISION_RE.finditer(prose):
            subject, body = (mt.group(1) or "").strip(), mt.group(2).strip()
            items.append(Item("decision", _clip(body), cid, mid, role, ts, basis, subject,
                              "current" if basis == "user" else "proposed", position=pos))
        for mt in TODO_RE.finditer(prose):
            items.append(Item("task", _clip(mt.group(1)), cid, mid, role, ts, basis, status="open", position=pos))
        for mt in CHECKBOX_RE.finditer(prose):
            done = mt.group(1).lower() == "x"
            items.append(Item("task", _clip(mt.group(2)), cid, mid, role, ts, basis,
                              status="done" if done else "open", position=pos))
        for mt in QUESTION_RE.finditer(prose):
            items.append(Item("question", _clip(mt.group(1)), cid, mid, role, ts, basis, status="open", position=pos))
        for mt in FENCE_RE.finditer(text):
            code = mt.group(2).rstrip()
            if code.strip():
                items.append(Item("snippet", code, cid, mid, role, ts, basis, lang=mt.group(1) or "", position=pos))
        for url in dict.fromkeys(URL_RE.findall(text)):
            items.append(Item("link", url, cid, mid, role, ts, basis, position=pos))

    # The person's own marks outrank anything inferred from wording.
    for b in bookmarks or []:
        m = by_id.get(b.get("message_id") or "")
        source_text = (b.get("quote") or (m or {}).get("text") or "").strip()
        note = (b.get("note") or "").strip()
        sm = _SUBJECT_NOTE.search(note)
        subject = sm.group(1) if sm else ""
        ts = b.get("created_at") or (m or {}).get("created_at")
        pos = (m or {}).get("position", 0.0)
        kind = b.get("kind") or "bookmark"
        if kind == "decision":
            items.append(Item("decision", _clip(note if note and not sm else source_text), cid, b.get("message_id"),
                              (m or {}).get("role", ""), ts, "marked", subject, "current", note=note, position=pos))
        elif kind == "todo":
            items.append(Item("task", _clip(note or source_text), cid, b.get("message_id"), (m or {}).get("role", ""),
                              ts, "marked", status="open", note=note, position=pos))
        elif kind == "snippet":
            fence = FENCE_RE.search((m or {}).get("text") or "")
            code = fence.group(2).rstrip() if fence else source_text
            items.append(Item("snippet", code, cid, b.get("message_id"), (m or {}).get("role", ""), ts, "marked",
                              lang=(fence.group(1) if fence else ""), note=note, position=pos))
        else:
            items.append(Item("highlight", _clip(source_text, 400), cid, b.get("message_id"), (m or {}).get("role", ""),
                              ts, "marked", note=note, position=pos))

    # An unanswered final question from the person is an open question.
    if messages and messages[-1]["role"] == "user" and (messages[-1].get("text") or "").rstrip().endswith("?"):
        last = messages[-1]
        items.append(Item("question", _clip(last["text"]), cid, last["id"], "user", last.get("created_at"), "user",
                          status="open", extra={"unanswered": True}, position=last.get("position", 0.0)))
    return items


def _order(item: Item) -> tuple[str, float]:
    return (item.ts or "", item.position)


def resolve(items: list[Item]) -> list[Item]:
    """Collapse duplicates and settle which decisions are current.

    Across ALL the given items (several conversations at once): decisions with the same
    explicit subject, or the same wording, supersede each other in time order. Only decisions
    the person took (``user`` / ``marked``) can supersede; an assistant proposal never does.
    """
    seen: dict[str, Item] = {}
    for it in items:
        k = it.key()
        prev = seen.get(k)
        if prev is None or (it.basis == "marked" and prev.basis != "marked"):
            seen[k] = it
    out = list(seen.values())
    groups: dict[str, list[Item]] = {}
    for it in out:
        if it.kind == "decision" and it.basis in ("user", "marked"):
            gk = "s:" + subject_key(it.subject) if it.subject else "t:" + subject_key(it.text)
            groups.setdefault(gk, []).append(it)
    for gk, group in groups.items():
        group.sort(key=_order)
        newest = group[-1]
        for older in group[:-1]:
            # Same subject, newer choice: the old one is superseded. Same words again: it was
            # only restated — still true, just not the latest mention.
            older.status = "superseded" if gk.startswith("s:") else "restated"
            older.superseded_by = f"{newest.conversation_id}#{newest.message_id}"
    return out


def summarize(items: list[Item]) -> dict[str, int]:
    c: dict[str, int] = {}
    for it in items:
        key = f"{it.kind}:{it.status}" if it.status else it.kind
        c[key] = c.get(key, 0) + 1
    return c


__all__ = ["Item", "extract", "resolve", "subject_key", "summarize"]
