"""Alert-once state for the "ending soon" giveaway reminder.

The daily deals run reminds you about un-grabbed giveaways about to expire — but a
reminder that repeats every day is the drip that trains you to ignore it. This
records which giveaways we've already reminded about (by key), so each is nudged
**once**. Keys that leave the feed (grabbed, or the giveaway ended) are pruned, so
the file stays small and a genuinely new giveaway can remind again.

One tiny JSON next to the ledger / deals state (`<config_dir>/games/expiry_reminders.json`).
Never raises — a reminder must never break the deals run.
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
    return d / "expiry_reminders.json"


class ExpiryReminderState:
    """Remembers which giveaways we've already sent an 'ending soon' nudge for."""

    def __init__(self, path: Path | None = None):
        self.path = path or _state_path()
        self._reminded: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        # load_json_safe rides out transient locks and quarantines a corrupt file,
        # degrading to {} — a reminder cache must never crash the deals run.
        data = load_json_safe(self.path)
        if isinstance(data, dict) and isinstance(data.get("reminded"), dict):
            return {str(k): str(v) for k, v in data["reminded"].items()}
        return {}

    def should_remind(self, key: str) -> bool:
        return bool(key) and key not in self._reminded

    def mark(self, key: str) -> None:
        if key:
            self._reminded[key] = datetime.now(timezone.utc).isoformat()

    def prune(self, current_keys: list[str]) -> None:
        """Drop keys no longer on offer so the file can't grow without bound (and a
        re-listed giveaway could remind again). Mirrors ``DealsState.reset_absent``."""
        current = set(current_keys)
        for key in list(self._reminded):
            if key not in current:
                del self._reminded[key]

    def save(self) -> None:
        try:
            atomic_write_json({"reminded": self._reminded}, self.path)
        except OSError:
            pass  # never break the deals run on a write hiccup
