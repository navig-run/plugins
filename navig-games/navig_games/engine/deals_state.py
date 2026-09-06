"""Alert-once state for the Steam deals watcher.

Records the best discount we've already notified per app so a daily scheduled
check doesn't re-alert the same sale. A better deal (deeper discount, or newly
free-to-keep) re-alerts; when a sale ends the app is cleared so the next sale
alerts again.
"""

from __future__ import annotations

from pathlib import Path

from navig.core.json_io import atomic_write_json, load_json_safe


def _state_path() -> Path:
    try:
        from navig.platform.paths import config_dir

        base = config_dir()
    except Exception:
        base = Path.home() / ".navig"
    d = base / "games"
    d.mkdir(parents=True, exist_ok=True)
    return d / "deals.json"


class DealsState:
    def __init__(self, path: Path | None = None):
        self.path = path or _state_path()
        self._seen: dict[str, int] = self._load()
        #: Apps we deliberately cleared this run (see reset_absent) — remembered so a
        #: merge at save time doesn't resurrect them from the file.
        self._removed: set[str] = set()

    def _load(self) -> dict[str, int]:
        # load_json_safe rides out transient locks and quarantines a corrupt file,
        # degrading to {}. Used both at construction and in the save-time merge.
        data = load_json_safe(self.path)
        if isinstance(data, dict) and isinstance(data.get("seen"), dict):
            try:
                return {str(k): int(v) for k, v in data["seen"].items()}
            except (TypeError, ValueError):
                return {}
        return {}

    def _effective(self, deal) -> int:
        return 100 if deal.is_free else deal.discount_pct

    def should_alert(self, deal) -> bool:
        """Alert only when this deal is better than the last one we announced."""
        return self._effective(deal) > self._seen.get(str(deal.appid), 0)

    def mark(self, deal) -> None:
        self._seen[str(deal.appid)] = self._effective(deal)

    def reset_absent(self, current_appids: list[int]) -> None:
        """Clear apps no longer on offer so a future sale re-alerts."""
        current = {str(a) for a in current_appids}
        for key in list(self._seen):
            if key not in current:
                del self._seen[key]
                self._removed.add(key)

    def save(self) -> None:
        """Merge onto whatever is on disk now, then write.

        A deals run loads this at the start and saves ~30s later at the end. The
        scheduled job (a subprocess) and a manual ``navig games deals notify`` can
        overlap, and a plain whole-file save would drop the other run's marks —
        re-announcing deals the user was already told about. Per app we keep the
        *strongest* discount either run announced (alert-once is monotonic), minus
        what we explicitly cleared.
        """
        merged = {k: v for k, v in self._load().items() if k not in self._removed}
        for appid, pct in self._seen.items():
            merged[appid] = max(pct, merged.get(appid, 0))
        self._seen = merged

        atomic_write_json({"seen": self._seen}, self.path)
