"""Consent-first investigation: authorization gate + evidence case directory.

Reuses the war-room-space framing (``registry/spaces/war-room-space/docs/
ETHICS-AND-AUTHORIZATION.md`` + ``arsenal/forensic/chain-of-custody.md`` +
``arsenal/legal/authorization-template.md``):

  * An investigative op is authorized only when a consent record exists with a
    non-empty **authorization reference** ("I own this device" is valid for a
    self-owned device), within its window. The gate refuses otherwise.
  * Evidence is written into a per-case directory; every artifact is hashed on
    collection (sha256 + size) and recorded in an append-only ``manifest.json``
    with the 5 W's (what / who / when-UTC / where / how). Originals are never
    mutated.

Investigative verbs (forensics acquire, spyware scan, private fs/media pull,
restore, root/jailbreak) call :meth:`ConsentGate.require` first.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from navig_mobile import config


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ── errors ───────────────────────────────────────────────────────────────────

class ConsentError(Exception):
    """Base for consent/authorization failures."""


class ConsentRequired(ConsentError):
    def __init__(self, udid: str, action: str):
        self.udid = udid
        self.action = action
        super().__init__(
            f"'{action}' needs recorded authorization for device {udid}. "
            f"Record it first:\n"
            f"  navig mobile consent record -u {udid} "
            f'--authorization "I own this device" --scope "<what\'s in scope>"\n'
            f"Investigating a device you don't own without authorization is illegal."
        )


class ConsentRefused(ConsentError):
    """The authorization gate refused to record consent (missing reference)."""


# ── the gate ─────────────────────────────────────────────────────────────────

class ConsentGate:
    """Thin wrapper over the store's consent ledger."""

    def __init__(self, store=None):
        self._store = store

    def _get_store(self):
        if self._store is not None:
            return self._store
        from navig_mobile.store import get_store

        return get_store()

    @staticmethod
    def normalize_until(until: str | None) -> str | None:
        """Normalize an expiry to a comparable UTC ISO stamp: a bare
        ``YYYY-MM-DD`` becomes end-of-day; a full ISO timestamp is kept. Raises
        ``ValueError`` on anything unparseable (never silently store a bad value
        that string-compares wrong → never-expires / instantly-expired)."""
        if not until:
            return None
        until = until.strip()
        try:
            datetime.strptime(until, "%Y-%m-%d")
            return f"{until}T23:59:59.999999Z"
        except ValueError:
            pass
        try:
            datetime.fromisoformat(until.replace("Z", "+00:00"))
            return until
        except ValueError as exc:
            raise ValueError(
                f"Unparseable --until {until!r}. Use YYYY-MM-DD or an ISO timestamp."
            ) from exc

    def record(
        self,
        *,
        udid: str,
        authorization_ref: str,
        scope: str = "",
        operator: str = "",
        until: str | None = None,
        note: str = "",
    ) -> None:
        ref = (authorization_ref or "").strip()
        if not ref:
            raise ConsentRefused(
                "REFUSED (authorization gate): an authorization reference is "
                "required. Use \"I own this device\" for a device you own, or a "
                "signed-authorization / ticket / bug-bounty-scope reference."
            )
        try:
            expires_at = self.normalize_until(until)
        except ValueError as exc:
            raise ConsentRefused(str(exc)) from exc
        self._get_store().record_consent(
            udid=udid, authorization_ref=ref, scope=scope,
            operator=operator or config.operator_email(),
            expires_at=expires_at, note=note,
        )

    def check(self, udid: str) -> dict[str, Any] | None:
        return self._get_store().active_consent(udid)

    def require(self, udid: str, action: str) -> dict[str, Any]:
        rec = self.check(udid)
        if not rec:
            raise ConsentRequired(udid, action)
        return rec

    def revoke(self, udid: str) -> int:
        return self._get_store().revoke_consent(udid)


# ── evidence case directory + chain-of-custody manifest ──────────────────────

@dataclass
class CaseDir:
    """A per-case evidence directory with an append-only hashed manifest."""

    case_id: str
    root: Path
    udid: str = ""
    platform: str = ""
    name: str = ""
    manifest: dict[str, Any] = field(default_factory=dict)

    MANIFEST_NAME = "manifest.json"

    @classmethod
    def create(
        cls,
        *,
        udid: str = "",
        platform: str = "",
        name: str = "",
        authorization_ref: str = "",
        scope: str = "",
        base: Path | None = None,
        case_id: str | None = None,
    ) -> "CaseDir":
        cid = case_id or _new_case_id(udid)
        base = base or (config.mobile_dir() / "cases")
        root = Path(base) / cid
        root.mkdir(parents=True, exist_ok=True)
        cd = cls(case_id=cid, root=root, udid=udid, platform=platform, name=name)
        cd.manifest = {
            "case_id": cid,
            "name": name,
            "udid": udid,
            "platform": platform,
            "operator": config.operator_email(),
            "host": socket.gethostname(),
            "authorization_ref": authorization_ref,
            "scope": scope,
            "created_at": _utcnow(),
            "artifacts": [],
            "events": [{"at": _utcnow(), "action": "case_created", "detail": name or cid}],
        }
        cd._save()
        return cd

    @classmethod
    def open(cls, root: Path) -> "CaseDir":
        root = Path(root)
        mf = root / cls.MANIFEST_NAME
        manifest = json.loads(mf.read_text(encoding="utf-8")) if mf.exists() else {}
        return cls(case_id=manifest.get("case_id", root.name), root=root,
                   udid=manifest.get("udid", ""), platform=manifest.get("platform", ""),
                   name=manifest.get("name", ""), manifest=manifest)

    # ── evidence ────────────────────────────────────────────────────────────
    def add_evidence(self, path: Path, *, tool: str = "", source: str = "",
                     note: str = "", copy: bool = False) -> dict[str, Any]:
        """Hash a produced artifact (file or dir) and append it to the manifest.

        With ``copy=True`` the artifact is copied into the case dir first (for
        externally-produced originals); by default artifacts already live under
        the case dir and are recorded in place. Originals are never mutated.
        """
        src = Path(path)
        if copy:
            dest = self.root / "evidence" / src.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dest)
            src = dest
        sha, size = _hash_path(src)
        try:
            rel = str(src.relative_to(self.root))
        except ValueError:
            rel = str(src)
        entry = {
            "evidence_id": f"evd_{len(self.manifest.get('artifacts', [])) + 1:04d}",
            "path": rel,
            "sha256": sha,
            "bytes": size,
            "collected_at": _utcnow(),
            "tool": tool,
            "source": source,
            "note": note,
        }
        self.manifest.setdefault("artifacts", []).append(entry)
        self.log_event("evidence_added", f"{entry['evidence_id']} {rel}")
        return entry

    def log_event(self, action: str, detail: str = "") -> None:
        self.manifest.setdefault("events", []).append(
            {"at": _utcnow(), "action": action, "detail": detail})
        self._save()

    def subdir(self, name: str) -> Path:
        d = self.root / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def verify(self) -> list[dict[str, Any]]:
        """Re-hash every recorded artifact and report integrity."""
        results = []
        for a in self.manifest.get("artifacts", []):
            p = self.root / a["path"]
            ok = False
            current = ""
            if p.exists():
                current, _ = _hash_path(p)
                ok = current == a["sha256"]
            results.append({"evidence_id": a["evidence_id"], "path": a["path"],
                            "ok": ok, "expected": a["sha256"], "current": current,
                            "missing": not p.exists()})
        return results

    def _save(self) -> None:
        (self.root / self.MANIFEST_NAME).write_text(
            json.dumps(self.manifest, indent=2, default=str), encoding="utf-8")


# ── helpers ──────────────────────────────────────────────────────────────────

def _new_case_id(seed: str = "") -> str:
    import uuid

    return "case_" + uuid.uuid4().hex[:8]


def _hash_path(path: Path) -> tuple[str, int]:
    """sha256 + size of a file, or a stable tree-hash + total size of a dir."""
    path = Path(path)
    if path.is_file():
        h = hashlib.sha256()
        size = 0
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
                size += len(chunk)
        return h.hexdigest(), size
    if path.is_dir():
        h = hashlib.sha256()
        size = 0
        for f in sorted(path.rglob("*")):
            if f.is_file():
                rel = str(f.relative_to(path)).replace("\\", "/")
                fh_hash, fsize = _hash_path(f)
                h.update(rel.encode("utf-8"))
                h.update(fh_hash.encode("ascii"))
                size += fsize
        return h.hexdigest(), size
    return "", 0
