"""Idempotent claimed-games ledger — the memory that makes claiming safe to
re-run on any schedule.

Stored as a small JSON file at ``<config_dir>/games/claimed.json``. Keyed by the
store-scoped :pyattr:`FreeGame.key`, so a game is never claimed twice and
"what's new since last run" is a set-difference.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from navig.core.json_io import (
    JsonReadError,
    atomic_write_json,
    load_json_for_update,
    load_json_safe,
)

from .claim.base import STATUS_CLAIMED, STATUS_GRABBED, STATUS_OWNED
from .models import FreeGame


def _ledger_path() -> Path:
    try:
        from navig.platform.paths import config_dir

        base = config_dir()
    except Exception:
        base = Path.home() / ".navig"
    d = base / "games"
    d.mkdir(parents=True, exist_ok=True)
    return d / "claimed.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Ledger:
    def __init__(self, path: Path | None = None):
        self.path = path or _ledger_path()
        self._data = self._load()

    @staticmethod
    def _valid(data: object) -> dict | None:
        """A well-formed ledger, or None (→ start fresh)."""
        if isinstance(data, dict) and isinstance(data.get("records"), dict):
            return data
        return None

    def _load(self) -> dict:
        """Construction read — degrading: an unreadable (transient lock, retried)
        or corrupt file yields a fresh ledger rather than crashing the claim run.
        Corrupt bytes are quarantined as ``claimed.json.corrupt`` by json_io."""
        return self._valid(load_json_safe(self.path)) or {"version": 1, "records": {}}

    def _save(self) -> None:
        atomic_write_json(self._data, self.path)

    def get(self, key: str) -> dict | None:
        return self._data["records"].get(key)

    #: Nothing left to do for this game — skip it on re-runs.
    SETTLED = (STATUS_CLAIMED, STATUS_OWNED, STATUS_GRABBED)

    def is_settled(self, key: str) -> bool:
        """True when the game is claimed, owned, or you grabbed it yourself.

        A grabbed game is settled on purpose: marking one doubles as "skip this",
        and a deliberate per-game claim still overrides it (``run_claim(only=…)``
        never consults the ledger).
        """
        rec = self.get(key)
        return bool(rec) and rec.get("status") in self.SETTLED

    def forget(self, key: str) -> bool:
        """Drop a record entirely (undo a grab). False when there was nothing to
        drop — or the store was transiently unreadable, in which case we refuse
        rather than risk wiping every other record."""
        try:
            self._reload()
        except JsonReadError:
            return False
        if self._data["records"].pop(key, None) is None:
            return False
        self._save()
        return True

    def _reload(self) -> None:
        """Re-read from disk immediately before a write.

        The claim runs in a *subprocess* while the daemon may be recording a grab,
        so two live ledgers exist. Loading once at construction and saving the whole
        file would let the later writer erase everything the other one recorded.

        Uses the *mutating* json_io loader, which RAISES ``JsonReadError`` on a
        persistent transient lock rather than returning an empty ledger — so a
        momentary AV/backup lock can't turn this write into a wipe of every record.
        """
        self._data = self._valid(load_json_for_update(self.path)) or {
            "version": 1,
            "records": {},
        }

    def record(self, game: FreeGame, status: str, note: str = "") -> None:
        try:
            self._reload()
        except JsonReadError:
            # Store transiently unreadable — skip persisting this record rather
            # than wipe every other claim. Claiming is idempotent, so it re-attempts
            # on the next run.
            return
        prev = self._data["records"].get(game.key, {})
        first_seen = prev.get("first_seen") or _now_iso()
        self._data["records"][game.key] = {
            "key": game.key,
            "store": game.store,
            "title": game.title,
            "url": game.url,
            "status": status,
            "note": note,
            "first_seen": first_seen,
            "updated": _now_iso(),
            "ends_at": game.ends_at,
        }
        self._save()

    def unseen(self, games: list[FreeGame]) -> list[FreeGame]:
        """Games not yet settled (claimed/owned) — the ones worth attempting."""
        return [g for g in games if not self.is_settled(g.key)]

    def all(self) -> list[dict]:
        return sorted(
            self._data["records"].values(),
            key=lambda r: r.get("updated", ""),
            reverse=True,
        )
