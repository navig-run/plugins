"""The provider-neutral conversation model.

A conversation's identity is ``<provider>:<native id>`` — the id the source site uses — so an
export import, a live capture and a bookmark of the same chat land on one record.

``completeness`` is how much of the conversation the library actually holds, ranked:

* ``url_only`` — a bookmark: the link and a title, no messages.
* ``partial``  — some messages (a selection, a capture taken mid-stream).
* ``rendered`` — every message of the branch the page showed, captured live. Other branches,
  hidden tool turns and attachments may be missing.
* ``complete`` — the provider's own account export: every branch it exported.

A merge only ever raises it (:func:`merge_completeness`); nothing reports a partial record as
complete.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

PROVIDERS = ("chatgpt", "claude", "gemini", "grok", "other")
COMPLETENESS = ("url_only", "partial", "rendered", "complete")
PROVENANCE = ("export", "live", "manual")
ROLES = ("user", "assistant", "system", "tool")
BOOKMARK_KINDS = ("bookmark", "decision", "snippet", "todo")

_RANK = {c: i for i, c in enumerate(COMPLETENESS)}


def merge_completeness(existing: str | None, incoming: str) -> str:
    """The better of the two — a merge never makes a record look less complete than it is."""
    if existing not in _RANK:
        return incoming
    return existing if _RANK[existing] >= _RANK.get(incoming, 0) else incoming


def conversation_id(provider: str, native_id: str) -> str:
    return f"{provider}:{native_id}"


def url_native_id(url: str) -> str:
    """A stable id for a chat known only by its link (no native id could be read)."""
    return "url-" + hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:16]


@dataclass
class MessageIn:
    """One message as an importer or capture hands it to the store."""

    id: str
    role: str
    text: str
    parent_id: str | None = None
    content_type: str = "text"
    created_at: str | None = None
    model: str | None = None
    has_code: bool = False
    on_active_branch: bool = True
    parts: list[Any] | None = None  # structured content kept for exact JSON export
    attachments: list[dict[str, Any]] = field(default_factory=list)

    def digest(self) -> str:
        return hashlib.sha256(f"{self.id}\x1f{self.role}\x1f{self.text}".encode()).hexdigest()


@dataclass
class ConversationIn:
    """A whole (or partial) conversation on its way into the store."""

    provider: str
    native_id: str
    title: str
    url: str
    messages: list[MessageIn]
    completeness: str
    provenance: str
    created_at: str | None = None
    updated_at: str | None = None
    account_scope: str = ""
    remote_project_id: str | None = None
    remote_project_name: str | None = None
    gizmo_id: str | None = None
    archived_remote: bool = False

    @property
    def id(self) -> str:
        return conversation_id(self.provider, self.native_id)

    def content_hash(self) -> str:
        h = hashlib.sha256(self.title.encode("utf-8"))
        for m in self.messages:
            h.update(m.digest().encode())
        return h.hexdigest()


__all__ = [
    "BOOKMARK_KINDS",
    "COMPLETENESS",
    "PROVENANCE",
    "PROVIDERS",
    "ROLES",
    "ConversationIn",
    "MessageIn",
    "conversation_id",
    "merge_completeness",
    "url_native_id",
]
