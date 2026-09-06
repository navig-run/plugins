"""DeviceInventoryStore — records devices seen by navig-mobile and a catalog of
backups taken. SQLite via core's ``BaseStore`` (delegates connection/PRAGMA/write
serialization to ``navig.storage.Engine``).

DB lives at ``<data_dir>/mobile.db`` (honors ``NAVIG_DATA_DIR`` for test
isolation). Import is lazy — ``BaseStore`` is only pulled in when the store is
first constructed, so ``navig help`` stays fast.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from navig.store.base import BaseStore

from navig_mobile import config


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class DeviceInventoryStore(BaseStore):
    SCHEMA_VERSION = 2

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices (
                udid        TEXT PRIMARY KEY,
                platform    TEXT NOT NULL,
                name        TEXT,
                model       TEXT,
                os_version  TEXT,
                serial      TEXT,
                first_seen  TEXT NOT NULL,
                last_seen   TEXT NOT NULL,
                extra       TEXT
            );

            CREATE TABLE IF NOT EXISTS backups (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                udid        TEXT NOT NULL,
                platform    TEXT,
                path        TEXT NOT NULL,
                encrypted   INTEGER NOT NULL DEFAULT 0,
                sha256      TEXT,
                bytes       INTEGER,
                created     TEXT NOT NULL,
                note        TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_backups_udid ON backups(udid);

            -- Stage 2: authorization ledger (consent-first investigation).
            CREATE TABLE IF NOT EXISTS consent (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                udid              TEXT NOT NULL,
                authorization_ref TEXT NOT NULL,
                scope             TEXT,
                operator          TEXT,
                granted_at        TEXT NOT NULL,
                expires_at        TEXT,
                revoked           INTEGER NOT NULL DEFAULT 0,
                note              TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_consent_udid ON consent(udid);

            -- Stage 2: case index (evidence lives on disk under each case dir).
            CREATE TABLE IF NOT EXISTS cases (
                case_id     TEXT PRIMARY KEY,
                name        TEXT,
                udid        TEXT,
                platform    TEXT,
                path        TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'open',
                created_at  TEXT NOT NULL,
                note        TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_cases_udid ON cases(udid);
            """
        )

    def _migrate(self, conn: sqlite3.Connection, from_v: int, to_v: int) -> None:  # noqa: ARG002
        # v1 → v2 is purely additive: `consent` + `cases` tables are created by
        # `_create_schema` (CREATE TABLE IF NOT EXISTS runs before this on every
        # init), so there is nothing to backfill. Left explicit for future steps.
        return

    # ── devices ─────────────────────────────────────────────────────────────
    def record_device(
        self,
        *,
        udid: str,
        platform: str,
        name: str = "",
        model: str = "",
        os_version: str = "",
        serial: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        now = _utcnow()
        self._write(
            """
            INSERT INTO devices (udid, platform, name, model, os_version, serial,
                                 first_seen, last_seen, extra)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(udid) DO UPDATE SET
                platform=excluded.platform,
                name=COALESCE(NULLIF(excluded.name, ''), devices.name),
                model=COALESCE(NULLIF(excluded.model, ''), devices.model),
                os_version=COALESCE(NULLIF(excluded.os_version, ''), devices.os_version),
                serial=COALESCE(NULLIF(excluded.serial, ''), devices.serial),
                last_seen=excluded.last_seen,
                extra=excluded.extra
            """,
            (udid, platform, name, model, os_version, serial, now, now,
             json.dumps(extra or {})),
        )

    def list_devices(self) -> list[dict[str, Any]]:
        rows = self._read_all("SELECT * FROM devices ORDER BY last_seen DESC")
        return [self._row(r) for r in rows]

    # ── backups ─────────────────────────────────────────────────────────────
    def record_backup(
        self,
        *,
        udid: str,
        path: str,
        platform: str = "",
        encrypted: bool = False,
        sha256: str = "",
        size_bytes: int | None = None,
        note: str = "",
    ) -> int:
        cur = self._write(
            """
            INSERT INTO backups (udid, platform, path, encrypted, sha256, bytes, created, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (udid, platform, path, 1 if encrypted else 0, sha256, size_bytes,
             _utcnow(), note),
        )
        return int(cur.lastrowid or 0)

    def list_backups(self, udid: str | None = None) -> list[dict[str, Any]]:
        if udid:
            rows = self._read_all(
                "SELECT * FROM backups WHERE udid=? ORDER BY created DESC", (udid,)
            )
        else:
            rows = self._read_all("SELECT * FROM backups ORDER BY created DESC")
        return [self._row(r) for r in rows]

    # ── consent (authorization ledger) ──────────────────────────────────────
    def record_consent(
        self,
        *,
        udid: str,
        authorization_ref: str,
        scope: str = "",
        operator: str = "",
        expires_at: str | None = None,
        note: str = "",
    ) -> None:
        self._write(
            """
            INSERT INTO consent (udid, authorization_ref, scope, operator,
                                 granted_at, expires_at, revoked, note)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (udid, authorization_ref, scope, operator, _utcnow(), expires_at, note),
        )

    def active_consent(self, udid: str, *, now: str | None = None) -> dict[str, Any] | None:
        """The most recent non-revoked, non-expired consent record for a device."""
        now = now or _utcnow()
        row = self._read_one(
            """
            SELECT * FROM consent
            WHERE udid=? AND revoked=0 AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY granted_at DESC LIMIT 1
            """,
            (udid, now),
        )
        return dict(row) if row else None

    def revoke_consent(self, udid: str) -> int:
        cur = self._write("UPDATE consent SET revoked=1 WHERE udid=? AND revoked=0", (udid,))
        return cur.rowcount if cur.rowcount is not None else 0

    # ── cases ───────────────────────────────────────────────────────────────
    def record_case(
        self,
        *,
        case_id: str,
        path: str,
        name: str = "",
        udid: str = "",
        platform: str = "",
        note: str = "",
    ) -> None:
        self._write(
            """
            INSERT INTO cases (case_id, name, udid, platform, path, status, created_at, note)
            VALUES (?, ?, ?, ?, ?, 'open', ?, ?)
            ON CONFLICT(case_id) DO UPDATE SET
                name=excluded.name, udid=excluded.udid, platform=excluded.platform,
                path=excluded.path, note=excluded.note
            """,
            (case_id, name, udid, platform, path, _utcnow(), note),
        )

    def list_cases(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._read_all("SELECT * FROM cases ORDER BY created_at DESC")]

    def get_case(self, case_id: str) -> dict[str, Any] | None:
        row = self._read_one("SELECT * FROM cases WHERE case_id=?", (case_id,))
        return dict(row) if row else None

    @staticmethod
    def _row(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        if "extra" in d and isinstance(d["extra"], str):
            try:
                d["extra"] = json.loads(d["extra"] or "{}")
            except Exception:
                d["extra"] = {}
        return d


_STORE: DeviceInventoryStore | None = None


def get_store(db_path: Path | None = None) -> DeviceInventoryStore:
    """Singleton accessor. Pass ``db_path`` in tests for isolation."""
    global _STORE
    if db_path is not None:
        return DeviceInventoryStore(db_path)
    if _STORE is None:
        _STORE = DeviceInventoryStore(config.db_path())
    return _STORE
