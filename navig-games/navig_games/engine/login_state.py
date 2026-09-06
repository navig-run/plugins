"""Alert-once state for the Epic sign-in.

A scheduled claim runs daily, so a session that has expired would otherwise fire
the same "not signed in" alert on *every* run — the drip that trains you to mute
the one notification that matters. This records which expired batch we've already
alerted about, keyed by the set of games waiting, so:

- an expired session alerts **once**, not once per day;
- a *new* week's free games re-alert even while still expired (a new batch is
  genuinely worth a nudge);
- a successful sign-in clears the state, so the *next* expiry alerts again.

One tiny JSON next to the ledger / deals state (`<config_dir>/games/login_alert.json`).
Never raises — a health note must never break a claim.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from navig.core.json_io import atomic_write_json, load_json_safe


def _state_path() -> Path:
    try:
        from navig.platform.paths import config_dir

        base = config_dir()
    except Exception:  # noqa: BLE001
        base = Path.home() / ".navig"
    d = base / "games"
    d.mkdir(parents=True, exist_ok=True)
    return d / "login_alert.json"


def _signature(keys: list[str]) -> str:
    """A stable fingerprint of the waiting batch, order-independent."""
    return "|".join(sorted(k for k in keys if k))


class LoginAlertState:
    """Remembers the last expired batch we alerted about (see module docstring)."""

    def __init__(self, path: Path | None = None):
        self.path = path or _state_path()
        self._data = self._load()

    def _load(self) -> dict:
        # load_json_safe rides out transient locks and quarantines a corrupt file,
        # degrading to {} — a health note must never break a claim.
        data = load_json_safe(self.path)
        return data if isinstance(data, dict) else {}

    def _save(self) -> None:
        # Atomic (temp + fsync + replace) via json_io: the claim SUBPROCESS writes
        # this while other processes may read it; a half-write must not be seen.
        try:
            atomic_write_json(self._data, self.path)
        except OSError:
            pass

    def should_alert_expiry(self, keys: list[str]) -> bool:
        """True only when this expired batch hasn't already been alerted.

        An empty batch never alerts (nothing is actually waiting).
        """
        sig = _signature(keys)
        return bool(sig) and sig != self._data.get("expired_signature")

    def mark_expiry_alerted(self, keys: list[str]) -> None:
        self._data = {
            "expired_signature": _signature(keys),
            "alerted_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    def clear_expiry(self) -> None:
        """Re-arm: the session works again, so a future expiry should alert."""
        if self._data:
            self._data = {}
            self._save()
