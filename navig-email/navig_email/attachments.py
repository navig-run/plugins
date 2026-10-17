"""Email attachments → the space's paperwork intake, with where they came from.

The mailroom does not file documents itself: navig-cabinet already reads, classifies and files
whatever lands in ``<space>/inbox/`` (its ``courrier:scan`` job), with its own ownership checks
and its own undo. This module only puts a document there — local disk, never an external
service — and records its provenance (account, message id, sender, subject, sha256) in
``mailroom/ledger/attachments.jsonl`` so a filed invoice can always be traced back to its email.

Only document types are taken (PDF, office files, images), each under 25 MB, with a sanitised
name; the same bytes are never saved twice. A message whose attachment was saved is marked so
the briefing's clean-up never trashes it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from navig_email.messages import address

DOC_TYPES = re.compile(r"\.(pdf|docx?|xlsx?|odt|ods|png|jpe?g|heic|tiff?)$", re.IGNORECASE)
MAX_BYTES = 25 * 1024 * 1024
INTAKE_DIR = "inbox"


@dataclass
class Saved:
    saved: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out = [payload] if payload.get("filename") else []
    for p in payload.get("parts") or []:
        out.extend(_parts(p))
    return out


def safe_name(name: str) -> str:
    stem = Path(name or "attachment").name
    stem = re.sub(r"[^\w.\- ]+", "_", stem, flags=re.UNICODE).strip(" .") or "attachment"
    return stem[:120]


def _ledger(paths: Any) -> Path:
    return paths.ledger("attachments")


def known_hashes(paths: Any) -> set[str]:
    f = _ledger(paths)
    if not f.exists():
        return set()
    out = set()
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            out.add(json.loads(line)["sha256"])
        except (ValueError, KeyError):
            continue
    return out


async def save_from_message(connector, message_id: str, *, paths: Any, account: str, dry_run: bool = False) -> Saved:
    res = Saved()
    msg = await connector.get_message(message_id, format="full")
    hdr = {h["name"].lower(): h["value"] for h in (msg.get("payload") or {}).get("headers") or []}
    intake = paths.space_root / INTAKE_DIR
    seen = known_hashes(paths)
    for part in _parts(msg.get("payload") or {}):
        name = str(part.get("filename") or "")
        if not DOC_TYPES.search(name):
            res.skipped.append(f"{name or '(unnamed)'}: not a document type")
            continue
        body = part.get("body") or {}
        if int(body.get("size") or 0) > MAX_BYTES:
            res.skipped.append(f"{name}: larger than 25 MB")
            continue
        try:
            if body.get("data"):
                raw = str(body["data"])
                data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
            elif body.get("attachmentId") and callable(getattr(connector, "get_attachment", None)):
                data = await connector.get_attachment(message_id, body["attachmentId"])
            else:
                res.skipped.append(f"{name}: this mail backend cannot download attachments")
                continue
        except Exception as exc:  # noqa: BLE001 — one attachment failing does not stop the others
            res.errors.append(f"{name}: {exc}")
            continue
        if len(data) > MAX_BYTES:
            res.skipped.append(f"{name}: larger than 25 MB")
            continue
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen:
            res.skipped.append(f"{name}: already saved")
            continue
        stamp = date.today().strftime("%Y%m%d")  # the operator's own day, like paperwork's names
        target = intake / f"{stamp}-mail-{safe_name(name)}"
        n = 1
        while target.exists():
            target = intake / f"{stamp}-mail-{n}-{safe_name(name)}"
            n += 1
        if not paths.contains(target):
            res.errors.append(f"{name}: refused a path outside the space")
            continue
        row = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "account": account,
               "message_id": message_id, "from": address(hdr.get("from", "")), "subject": hdr.get("subject", "")[:200],
               "filename": name, "saved_as": str(target.relative_to(paths.space_root)), "sha256": digest,
               "bytes": len(data)}
        if not dry_run:
            intake.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            _ledger(paths).parent.mkdir(parents=True, exist_ok=True)
            with _ledger(paths).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            _protect(account, message_id)
        seen.add(digest)
        res.saved.append(row)
    return res


def _protect(account: str, message_id: str) -> None:
    """A message we filed a document from is never trashed by the briefing's clean-up."""
    from navig_email.store import get_store
    from navig_email.outbox import iso, now_utc

    get_store()._write(
        "INSERT INTO lifecycle (account, message_id, state, updated_at, note) VALUES (?, ?, 'kept', ?, 'attachment filed') "
        "ON CONFLICT(account, message_id) DO UPDATE SET state='kept', trash_after=NULL, note='attachment filed'",
        (account, message_id, iso(now_utc())),
    )
