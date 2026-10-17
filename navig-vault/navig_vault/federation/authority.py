"""The Vault Authority — scoped, audited secret access for enrolled devices.

One install (Brain mode on the outpost, eventually) runs with
``vault.authority.enabled``; every other machine is a DEVICE with its own key
and an explicit list of grants. There is no fleet-wide token and no copy of the
vault anywhere else: a device receives exactly the values its grants name, one
request at a time, sealed to its own key.

Storage is the authority's own ``vault.db``:

* device HMAC keys are ordinary vault items (``federation/device/<id>``), so the
  VMK seals them and rotation, backup and audit already cover them;
* ``vault_fed_devices`` / ``vault_fed_grants`` / ``vault_fed_codes`` hold
  non-secret metadata (enrollment codes only as sha256);
* ``vault_fed_nonces`` makes a signature single-use, across restarts too.

Default deny. A label under ``federation/`` is never grantable — no pattern can
hand one device another device's key. Management (enroll codes, grant, revoke)
is a local operator API: it is called by the CLI on this machine, never over
the network.

Transport-agnostic: :meth:`handle` takes method/path/headers/body plus whether
the request came through a relay, and returns ``(status, json)``. The gateway
route is a thin adapter around it.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import secrets
import time
import uuid
from collections.abc import Callable

from ..core import acting_as
from . import protocol as P

__all__ = ["Authority", "DEVICE_KINDS", "normalize_pattern"]

DEVICE_KINDS = ("client", "server", "mini")
ENROLL_CODE_TTL_S = 600
NEVER_GRANTABLE = "federation/"
_DEVICE_LABEL = "federation/device/{}"
_MAX_BODY = 64 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vault_fed_devices (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    created_at    REAL NOT NULL,
    expires_at    REAL,
    revoked_at    REAL,
    last_seen     REAL,
    -- NULL until the machine redeems its code: the device exists (and can be
    -- granted) from `enroll` on, but holds no key and cannot authenticate yet.
    joined_at     REAL,
    device_ttl_s  INTEGER
);
CREATE TABLE IF NOT EXISTS vault_fed_grants (
    id           TEXT PRIMARY KEY,
    device_id    TEXT NOT NULL,
    pattern      TEXT NOT NULL,
    actions      TEXT NOT NULL DEFAULT 'read',
    cache_ttl_s  INTEGER NOT NULL DEFAULT 0,
    expires_at   REAL,
    granted_by   TEXT NOT NULL DEFAULT 'operator',
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fed_grants_device ON vault_fed_grants(device_id);
CREATE TABLE IF NOT EXISTS vault_fed_codes (
    code_hash  TEXT PRIMARY KEY,
    device_id  TEXT NOT NULL,
    expires    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS vault_fed_nonces (
    device_id  TEXT NOT NULL,
    nonce      TEXT NOT NULL,
    ts         REAL NOT NULL,
    PRIMARY KEY (device_id, nonce)
);
"""


#: Sliding one-minute budgets. Generous for real use (a deploy resolving a few
#: secrets), tight enough that guessing codes or hammering a stolen device id
#: costs minutes, not milliseconds.
RATE_PER_DEVICE = 120
RATE_AUTH_FAILURES = 20
RATE_ENROLL = 10
_WINDOW_S = 60.0


class _RateLimiter:
    """In-memory sliding-window counter per key (resets with the process — by design:
    it bounds load and guessing speed, it is not an account lockout)."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._hits: dict[str, list[float]] = {}

    def retry_after(self, key: str, limit: int) -> int:
        """Seconds to wait if *key* is over *limit* in the window, else 0 (and counts it)."""
        now = self._clock()
        hits = [t for t in self._hits.get(key, []) if t > now - _WINDOW_S]
        if len(hits) >= limit:
            self._hits[key] = hits
            return max(1, int(hits[0] + _WINDOW_S - now) + 1)
        hits.append(now)
        self._hits[key] = hits
        if len(self._hits) > 10_000:  # memory backstop under a flood of distinct keys
            self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > now - _WINDOW_S}
        return 0

    def blocked(self, key: str, limit: int) -> int:
        """Like retry_after, but does NOT count this call (for failure-only budgets)."""
        now = self._clock()
        hits = [t for t in self._hits.get(key, []) if t > now - _WINDOW_S]
        self._hits[key] = hits
        if len(hits) >= limit:
            return max(1, int(hits[0] + _WINDOW_S - now) + 1)
        return 0

    def record(self, key: str) -> None:
        self._hits.setdefault(key, []).append(self._clock())


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_pattern(pattern: str) -> str:
    """``vault://hosts/x/ssh`` / ``vault:host/x/*`` / ``host/x/*`` → a label glob."""
    from ..refs import LEGACY_PREFIX, SCHEME, _label_for  # noqa: PLC0415

    p = pattern.strip()
    if p.startswith(SCHEME):
        p = _label_for(p[len(SCHEME) :].strip("/"))
    elif p.startswith(LEGACY_PREFIX):
        p = p[len(LEGACY_PREFIX) :]
    if not p:
        raise ValueError("empty grant pattern")
    if p.startswith(NEVER_GRANTABLE):
        raise ValueError("federation/ labels (device keys) can never be granted")
    return p


class Authority:
    def __init__(self, vault, *, clock: Callable[[], float] = time.time) -> None:
        self.vault = vault
        self._clock = clock
        self._limits = _RateLimiter(clock)
        # executescript commits implicitly, so it runs outside any transaction.
        with vault._store._lock:
            vault._store._connect().executescript(_SCHEMA)

    # ── small helpers ────────────────────────────────────────────────────────

    def _q(self, sql: str, params: tuple = ()) -> list:
        with self.vault._store._lock:
            return self.vault._store._connect().execute(sql, params).fetchall()

    def _audit(self, label: str, action: str, device_id: str, detail: str = "") -> None:
        item = self.vault._store.get(label) if label and label != "-" else None
        self.vault._store.audit(
            item.id if item else f"label:{label}", action, actor=f"device:{device_id}", detail=detail
        )

    # ── operator API (local only) ────────────────────────────────────────────

    def create_enrollment(
        self, name: str, *, kind: str = "client", ttl_s: int = ENROLL_CODE_TTL_S,
        device_ttl_s: int | None = None,
    ) -> str:
        """Create a PENDING device and the single-use 128-bit code that activates it.

        The device can be granted right away (enroll → grant → join is the natural
        order); it holds no key and cannot authenticate until the code is redeemed.
        Re-enrolling a pending name issues a fresh code for the same device. Only
        the code's hash is kept.
        """
        if kind not in DEVICE_KINDS:
            raise ValueError(f"device kind must be one of {', '.join(DEVICE_KINDS)}")
        name = name.strip()[:80]
        if not name:
            raise ValueError("a device needs a name")
        code = secrets.token_urlsafe(16)
        now = self._clock()
        with self.vault._store.transaction() as conn:
            conn.execute("DELETE FROM vault_fed_codes WHERE expires <= ?", (now,))
            live = conn.execute(
                "SELECT id, joined_at FROM vault_fed_devices WHERE name = ? AND revoked_at IS NULL",
                (name,),
            ).fetchone()
            if live is not None and live["joined_at"] is not None:
                raise ValueError(f"a device named {name!r} is already enrolled — revoke it first")
            if live is not None:
                device_id = live["id"]
                conn.execute("DELETE FROM vault_fed_codes WHERE device_id = ?", (device_id,))
            else:
                device_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO vault_fed_devices (id, name, kind, created_at, device_ttl_s) VALUES (?,?,?,?,?)",
                    (device_id, name, kind, now, device_ttl_s),
                )
            conn.execute(
                "INSERT INTO vault_fed_codes (code_hash, device_id, expires) VALUES (?,?,?)",
                (_sha256(code), device_id, now + ttl_s),
            )
        return code

    def list_devices(self) -> list[dict]:
        rows = self._q("SELECT * FROM vault_fed_devices ORDER BY created_at")
        grants = self.grants()
        out = []
        for r in rows:
            d = dict(r)
            d["grants"] = [g for g in grants if g["device_id"] == d["id"]]
            d["state"] = self._device_state(d)
            out.append(d)
        return out

    def _device_state(self, d: dict) -> str:
        if d.get("revoked_at"):
            return "revoked"
        if d.get("joined_at") is None:
            return "pending"
        if d.get("expires_at") and d["expires_at"] <= self._clock():
            return "expired"
        return "active"

    def find_device(self, ref: str) -> dict | None:
        """A device by id, id prefix (≥6 chars) or exact name."""
        rows = [dict(r) for r in self._q("SELECT * FROM vault_fed_devices")]
        exact = [d for d in rows if d["id"] == ref or d["name"] == ref]
        if len(exact) == 1:
            return exact[0]
        pref = [d for d in rows if len(ref) >= 6 and d["id"].startswith(ref)]
        return pref[0] if len(pref) == 1 else None

    def revoke_device(self, device_id: str) -> bool:
        """Immediate: the next request from this device is refused. Its key is deleted."""
        with self.vault._store.transaction() as conn:
            cur = conn.execute(
                "UPDATE vault_fed_devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (self._clock(), device_id),
            )
            changed = cur.rowcount == 1
            conn.execute("DELETE FROM vault_fed_codes WHERE device_id = ?", (device_id,))
        self.vault.delete(_DEVICE_LABEL.format(device_id))
        if changed:
            self._audit("-", "device_revoked", device_id)
        return changed

    def grant(
        self, device_id: str, pattern: str, *, cache_ttl_s: int = 0,
        expires_in_s: int | None = None, actions: tuple[str, ...] = ("read",),
        granted_by: str = "operator",
    ) -> str:
        if not self._q("SELECT 1 FROM vault_fed_devices WHERE id = ? AND revoked_at IS NULL", (device_id,)):
            raise ValueError("no such device (or it was revoked)")
        glob = normalize_pattern(pattern)
        if cache_ttl_s < 0:
            raise ValueError("cache TTL cannot be negative")
        bad = set(actions) - {"read", "list"}
        if bad:
            raise ValueError(f"unknown action(s): {', '.join(sorted(bad))}")
        gid = str(uuid.uuid4())
        now = self._clock()
        with self.vault._store.transaction() as conn:
            conn.execute(
                "INSERT INTO vault_fed_grants (id, device_id, pattern, actions, cache_ttl_s, expires_at, granted_by, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (gid, device_id, glob, ",".join(sorted(set(actions))), int(cache_ttl_s),
                 now + expires_in_s if expires_in_s else None, granted_by, now),
            )
        self._audit("-", "grant", device_id, glob)
        return gid

    def revoke_grants(self, device_id: str, pattern: str | None = None) -> int:
        with self.vault._store.transaction() as conn:
            if pattern is None:
                cur = conn.execute("DELETE FROM vault_fed_grants WHERE device_id = ?", (device_id,))
            else:
                cur = conn.execute(
                    "DELETE FROM vault_fed_grants WHERE device_id = ? AND pattern = ?",
                    (device_id, normalize_pattern(pattern)),
                )
            n = cur.rowcount
        if n:
            self._audit("-", "grant_revoked", device_id, pattern or "*")
        return n

    def grants(self, device_id: str | None = None) -> list[dict]:
        if device_id is None:
            return [dict(r) for r in self._q("SELECT * FROM vault_fed_grants ORDER BY created_at")]
        return [dict(r) for r in self._q(
            "SELECT * FROM vault_fed_grants WHERE device_id = ? ORDER BY created_at", (device_id,)
        )]

    # ── authorization ────────────────────────────────────────────────────────

    def _live_grants(self, device_id: str, action: str) -> list[dict]:
        now = self._clock()
        return [
            g for g in self.grants(device_id)
            if action in g["actions"].split(",") and not (g["expires_at"] and g["expires_at"] <= now)
        ]

    def allowed(self, device_id: str, label: str, action: str = "read") -> dict | None:
        """The grant that allows *action* on *label*, or None. Never for federation/ labels."""
        if label.startswith(NEVER_GRANTABLE):
            return None
        matches = [g for g in self._live_grants(device_id, action) if fnmatch.fnmatchcase(label, g["pattern"])]
        if not matches:
            return None
        # The most conservative cache TTL of every matching grant wins.
        return min(matches, key=lambda g: g["cache_ttl_s"])

    # ── device-facing request handling ───────────────────────────────────────

    def _authenticate(self, method: str, path: str, headers, body: bytes) -> tuple[str | None, str]:
        """``(device_id, "")`` or ``(None, reason)``. The reason is for the audit log only."""
        device_id = headers.get(P.H_DEVICE, "")
        nonce = headers.get(P.H_NONCE, "")
        if not device_id or len(nonce) != 32:
            return None, "missing signature headers"
        rows = self._q("SELECT * FROM vault_fed_devices WHERE id = ?", (device_id,))
        if not rows:
            return None, "unknown device"
        device = dict(rows[0])
        state = self._device_state(device)
        if state != "active":
            return None, f"device {state}"
        try:
            ts = int(headers.get(P.H_TS, ""))
        except ValueError:
            return None, "bad timestamp"
        now = self._clock()
        if abs(now - ts) > P.SKEW_S:
            return None, "timestamp outside the allowed window"
        try:
            key = self.vault.get_bytes(_DEVICE_LABEL.format(device_id))
        except KeyError:
            return None, "device key missing"
        if not P.verify_signature(key, method, path, headers, body):
            return None, "bad signature"
        with self.vault._store.transaction() as conn:
            conn.execute("DELETE FROM vault_fed_nonces WHERE ts < ?", (now - 2 * P.SKEW_S,))
            try:
                conn.execute(
                    "INSERT INTO vault_fed_nonces (device_id, nonce, ts) VALUES (?,?,?)",
                    (device_id, nonce, now),
                )
            except Exception:  # noqa: BLE001 — sqlite3.IntegrityError: the nonce was used
                return None, "replayed nonce"
            conn.execute("UPDATE vault_fed_devices SET last_seen = ? WHERE id = ?", (now, device_id))
        return device_id, ""

    def handle(
        self, method: str, path: str, headers, body: bytes, *, relayed: bool, source: str = "-"
    ) -> tuple[int, dict]:
        """``source`` is the caller's address as the transport saw it (rate-limit key only)."""
        if len(body) > _MAX_BODY:
            return 413, {"error": "payload_too_large"}
        if path == "/api/vault/v1/enroll":
            wait = self._limits.retry_after(f"enroll:{source}", RATE_ENROLL)
            if wait:
                return 429, {"error": "rate_limited", "retry_after": wait}
            return self._enroll(body, relayed=relayed)
        claimed = headers.get(P.H_DEVICE, "?")[:64]
        wait = self._limits.blocked(f"authfail:{claimed}", RATE_AUTH_FAILURES)
        if wait:
            return 429, {"error": "rate_limited", "retry_after": wait}
        device_id, reason = self._authenticate(method, path, headers, body)
        if device_id is None:
            self._limits.record(f"authfail:{claimed}")
            self._audit("-", "auth_failed", claimed, reason)
            if reason == "timestamp outside the allowed window":
                # Said apart from every other failure: a wrong clock is an operations
                # fault, not a revocation, and must not make a device wipe its cache.
                # The authority's time is not a secret.
                return 401, {"error": "clock_skew", "time": self._clock()}
            return 401, {"error": "unauthorized"}
        wait = self._limits.retry_after(f"device:{device_id}", RATE_PER_DEVICE)
        if wait:
            return 429, {"error": "rate_limited", "retry_after": wait}
        try:
            req = json.loads(body or b"{}")
        except ValueError:
            return 400, {"error": "bad_json"}
        seal = headers.get(P.H_SEAL, "aes-gcm")
        if seal == "none" and relayed:
            return 400, {"error": "sealing_required_over_relay"}
        nonce = headers.get(P.H_NONCE, "")
        with acting_as(f"device:{device_id}"):
            if path == "/api/vault/v1/resolve":
                return self._resolve(device_id, req, nonce, seal)
            if path == "/api/vault/v1/list":
                return self._list(device_id)
            if path == "/api/vault/v1/status":
                return self._status(device_id)
            if path == "/api/vault/v1/sync":
                return self._sync(device_id, req)
        return 404, {"error": "not_found"}

    def _enroll(self, body: bytes, *, relayed: bool) -> tuple[int, dict]:
        # The response carries the new device key. Through a TLS-terminating relay
        # that key would pass the relay in the clear, so enrollment needs a direct
        # path (loopback, LAN, or an SSH forward). Everything after is signed+sealed.
        if relayed:
            return 403, {"error": "enroll_requires_direct_connection"}
        try:
            code = str(json.loads(body or b"{}").get("code", ""))
        except ValueError:
            return 400, {"error": "bad_json"}
        now = self._clock()
        key = secrets.token_bytes(32)
        with self.vault._store.transaction() as conn:
            row = conn.execute(
                "SELECT d.* FROM vault_fed_codes c JOIN vault_fed_devices d ON d.id = c.device_id "
                "WHERE c.code_hash = ? AND c.expires > ? AND d.revoked_at IS NULL AND d.joined_at IS NULL",
                (_sha256(code), now),
            ).fetchone()
            if row is None or not code:
                return 403, {"error": "invalid_or_expired_code"}
            device = dict(row)
            conn.execute("DELETE FROM vault_fed_codes WHERE device_id = ?", (device["id"],))
            expires_at = now + device["device_ttl_s"] if device["device_ttl_s"] else None
            conn.execute(
                "UPDATE vault_fed_devices SET joined_at = ?, expires_at = ? WHERE id = ?",
                (now, expires_at, device["id"]),
            )
        self.vault.put(_DEVICE_LABEL.format(device["id"]), key, metadata={"federation": "device-key"})
        self._audit("-", "device_enrolled", device["id"], device["name"])
        return 200, {
            "device_id": device["id"],
            "device_key": base64.b64encode(key).decode("ascii"),
            "name": device["name"],
            "kind": device["kind"],
            "expires_at": expires_at,
        }

    def _resolve(self, device_id: str, req: dict, nonce: str, seal: str) -> tuple[int, dict]:
        from ..refs import parse_ref  # noqa: PLC0415

        raw = str(req.get("ref") or req.get("label") or "")
        try:
            ref = parse_ref(raw if raw.startswith("vault:") else "vault:" + raw)
        except ValueError:
            return 400, {"error": "bad_reference"}
        grant = self.allowed(device_id, ref.label, "read")
        if grant is None:
            self._audit(ref.label, "denied", device_id, "no grant")
            return 403, {"error": "forbidden"}
        item = self.vault._store.get(ref.label)
        if item is None:
            return 404, {"error": "not_found"}
        from ..refs import resolve_ref  # noqa: PLC0415

        try:
            value = resolve_ref(self.vault, ref)
        except ValueError:
            return 404, {"error": "no_such_field"}
        version = ref.version or item.version
        out: dict = {"label": ref.label, "version": version, "cache_ttl_s": grant["cache_ttl_s"]}
        if seal == "none":
            out["value"] = value
        else:
            key = self.vault.get_bytes(_DEVICE_LABEL.format(device_id))
            out["sealed"] = P.seal_value(key, nonce, ref.label, version, value)
        return 200, out

    def _list(self, device_id: str) -> tuple[int, dict]:
        labels = [
            i.label for i in self.vault._store.list()
            if self.allowed(device_id, i.label, "list") or self.allowed(device_id, i.label, "read")
        ]
        return 200, {"labels": labels}

    def _status(self, device_id: str) -> tuple[int, dict]:
        d = dict(self._q("SELECT * FROM vault_fed_devices WHERE id = ?", (device_id,))[0])
        return 200, {
            "device": {"id": d["id"], "name": d["name"], "kind": d["kind"], "expires_at": d["expires_at"]},
            "grants": [
                {k: g[k] for k in ("pattern", "actions", "cache_ttl_s", "expires_at")}
                for g in self._live_grants(device_id, "read") + self._live_grants(device_id, "list")
            ],
            "time": self._clock(),
        }

    def _sync(self, device_id: str, req: dict) -> tuple[int, dict]:
        """Which of the device's cached labels it may still hold, at which version."""
        keep: dict[str, dict] = {}
        drop: list[str] = []
        for label in [str(x) for x in (req.get("labels") or [])][:500]:
            grant = self.allowed(device_id, label, "read")
            item = self.vault._store.get(label)
            if grant is None or item is None or grant["cache_ttl_s"] <= 0:
                drop.append(label)
            else:
                keep[label] = {"version": item.version, "cache_ttl_s": grant["cache_ttl_s"]}
        return 200, {"keep": keep, "drop": drop, "time": self._clock()}
