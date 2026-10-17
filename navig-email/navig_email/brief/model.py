"""One extracted item and its identity across emails and across briefings."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

KINDS = ("product", "event", "job", "article", "task")

#: Query parameters that only track the click; dropping them makes two links to the same page equal.
_TRACKING = re.compile(r"^(?:utm_\w+|ref|ref_src|mc_[ce]id|fbclid|gclid|igshid|mkt_tok|ph_notification_id)$", re.I)


def canonical_url(url: str) -> str:
    """Strip tracking parameters and fragments; lowercase the host."""
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", query, ""))


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9а-яё]+", " ", t).strip()


@dataclass
class Source:
    account: str
    message_id: str
    subject: str = ""
    sender: str = ""
    date: str = ""

    def key(self) -> tuple[str, str]:
        return (self.account, self.message_id)


@dataclass
class Item:
    kind: str
    title: str
    summary: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    links: dict[str, str] = field(default_factory=dict)  # role → url (primary, register, apply, website…)
    sources: list[Source] = field(default_factory=list)
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)

    def fingerprint(self) -> str:
        """Same thing ⇔ same fingerprint. Similar names with different links or places stay apart."""
        anchor = ""
        primary = canonical_url(self.links.get("primary", ""))
        if self.kind == "product" and primary:
            anchor = urlsplit(primary).netloc + urlsplit(primary).path
        elif self.kind == "job":
            anchor = (urlsplit(primary).path if primary else "") + "|" + normalize(str(self.fields.get("location", "")))
        elif self.kind == "event":
            anchor = str(self.fields.get("date", "")) + "|" + normalize(str(self.fields.get("city", "")))
        elif primary:
            anchor = urlsplit(primary).netloc + urlsplit(primary).path
        raw = f"{self.kind}|{normalize(self.title)}|{anchor}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["fingerprint"] = self.fingerprint()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Item:
        return cls(
            kind=d["kind"], title=d["title"], summary=d.get("summary", ""), fields=dict(d.get("fields") or {}),
            links=dict(d.get("links") or {}), sources=[Source(**s) for s in d.get("sources") or []],
            score=float(d.get("score") or 0.0), reasons=list(d.get("reasons") or []),
        )

    def merge(self, other: Item) -> None:
        """Absorb *other* (the same item from another email): union of sources, fill gaps."""
        for s in other.sources:
            if s.key() not in {x.key() for x in self.sources}:
                self.sources.append(s)
        for k, v in other.fields.items():
            if v not in (None, "", [], {}) and self.fields.get(k) in (None, "", [], {}):
                self.fields[k] = v
        for k, v in other.links.items():
            self.links.setdefault(k, v)
        if len(other.summary) > len(self.summary):
            self.summary = other.summary
