"""Plain data shapes shared by the store, the importers, the exporter and the planner."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Where a tag came from. A re-run of an automatic tagger replaces only its own tags.
TAG_SOURCES = ("user", "ai", "rule")

#: Folder levels are joined with "/"; a literal "/" or "\\" inside a name is escaped.
FOLDER_SEP = "/"


def join_folder(parts: list[str]) -> str:
    """``["Dev", "A/B testing"]`` -> ``"Dev/A\\/B testing"`` (round-trips through split)."""
    out = []
    for part in parts:
        name = str(part).strip()
        if not name:
            continue
        out.append(name.replace("\\", "\\\\").replace("/", "\\/"))
    return FOLDER_SEP.join(out)


def split_folder(path: str | None) -> list[str]:
    """Inverse of :func:`join_folder`; empty segments are dropped."""
    if not path:
        return []
    parts: list[str] = []
    buf: list[str] = []
    escaped = False
    for ch in path:
        if escaped:
            buf.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == FOLDER_SEP:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if escaped:
        buf.append("\\")
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def folder_depth(path: str | None) -> int:
    return len(split_folder(path))


@dataclass
class ParsedBookmark:
    """One bookmark read from an export or a browser profile, before it is stored."""

    url: str
    title: str = ""
    notes: str = ""
    folder: str = ""
    tags: list[str] = field(default_factory=list)
    added: int | None = None  # unix seconds, when the source records it


@dataclass
class Bookmark:
    id: str
    url: str
    url_normalized: str = ""
    title: str | None = None
    notes: str | None = None
    folder_path: str = ""
    tags: dict[str, str] = field(default_factory=dict)  # tag -> source (user|ai|rule)
    vault_cred_id: str | None = None
    visit_count: int = 0
    last_visited: str | None = None
    http_status: int | None = None
    final_url: str | None = None
    checked_at: str | None = None
    check_error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def tag_list(self) -> list[str]:
        return sorted(self.tags)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "title": self.title,
            "notes": self.notes,
            "folder": self.folder_path,
            "tags": [{"tag": t, "source": s} for t, s in sorted(self.tags.items())],
            "vault_cred_id": self.vault_cred_id,
            "visit_count": self.visit_count,
            "last_visited": self.last_visited,
            "http_status": self.http_status,
            "final_url": self.final_url,
            "checked_at": self.checked_at,
            "check_error": self.check_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
