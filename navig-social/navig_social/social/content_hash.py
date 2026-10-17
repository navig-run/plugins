"""Content identity — the approval-binding hash (spec: SCHEMA wave1 SOCIAL-CONTRACT.md §3).

Two functions, two jobs:

``contract_hash(manifest)`` implements the §3 algorithm **byte-for-byte** so a
SCHEMA-built contract manifest and a NAVIG-side recomputation produce the SAME
``sha256:…``. The fixture in ``tests/social/fixtures/contract_hash.json`` pins
that cross-repo parity: the payload below hashed by SCHEMA's
``navig_contract.content_hash`` must equal what this module returns, forever.

``of_post(content, targets)`` binds a *Studio* post's own content (title/body/
per-network captions/media ids/targets) with the same canonical JSON rules —
stable identity, changes whenever content changes. Schedule, timezone,
approval/expiry policies and publication prefs are deliberately EXCLUDED:
rescheduling or changing a preference must never void an approval; only text,
media or destination changes may (the brief's invalidation triggers).

Media entries contribute only when they carry ``state == "built"`` and a
``sha256`` (contract manifests); a ``derived`` entry (rendered at freeze time)
is listed for humans but excluded — its bytes are protected separately by the
frozen-artifact checks on the SCHEMA side.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = ["contract_hash", "of_post", "canonical_dumps"]


def canonical_dumps(obj: Any) -> str:
    """The one serialization both functions hash: sorted keys, compact, UTF-8."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def contract_hash(manifest: dict[str, Any]) -> str:
    """The SOCIAL-CONTRACT §3 hash of a contract manifest (``sha256:…``)."""
    media = sorted(
        ({"role": m["role"], "sha256": m["sha256"]}
         for m in manifest.get("media", [])
         if m.get("state") == "built" and m.get("sha256")),
        key=lambda m: m["role"],
    )
    thumbs = sorted(
        ({"role": t["role"], "sha256": t["sha256"]}
         for t in manifest.get("thumbs", [])
         if t.get("sha256")),
        key=lambda t: t["role"],
    )
    payload = {
        "titles": manifest.get("titles", {}),
        "captions": manifest.get("captions", {}),
        "language": manifest.get("language", "en"),
        "hashtags": sorted(manifest.get("hashtags", [])),
        "media": media,
        "thumbs": thumbs,
        "subtitles": manifest.get("subtitles", []),
        "destinations": sorted(
            ({"network": d["network"], "account": d.get("account", "")}
             for d in manifest.get("destinations", [])),
            key=lambda d: d["network"],
        ),
    }
    return "sha256:" + hashlib.sha256(canonical_dumps(payload).encode("utf-8")).hexdigest()


def of_post(content: dict[str, Any] | None, targets: list[Any] | None = None) -> str:
    """Hash a Studio post's content + targets — approval binds this value.

    Accepts an explicit ``content_hash`` in the content (a Stage-C contract
    passthrough) so SCHEMA-supplied manifests keep their own pinned identity.
    """
    content = content or {}
    if isinstance(content, dict) and content.get("content_hash"):
        return str(content["content_hash"])
    payload = {
        "title": content.get("title", ""),
        "body": content.get("body", ""),
        "per_network": content.get("per_network") or {},
        "hashtags": sorted(content.get("hashtags") or []),
        "media": content.get("media") or [],
        "language": content.get("language", ""),
        "targets": sorted(
            ({"network": (t.get("network") if isinstance(t, dict) else str(t)),
              "target": (t.get("target", "") if isinstance(t, dict) else "")}
             for t in (targets or [])),
            key=lambda t: (t["network"], t["target"]),
        ),
    }
    return "sha256:" + hashlib.sha256(canonical_dumps(payload).encode("utf-8")).hexdigest()
