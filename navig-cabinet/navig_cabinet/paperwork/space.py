"""Resolve a navig space to its root, and own every artifact path underneath it.

One rule shapes this module: **nothing this plugin writes may land outside the space
it was pointed at.** Plans, receipts, the quarantine and the ledger all live under
``<space>/.navig/paperwork/``, so a migration is one self-contained, recoverable unit
and a sibling space is never a write target.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


def resolve_space_root(space: str) -> Path:
    """Resolve a space NAME (or an absolute path to one) to its root dir.

    Raises ``ValueError`` if unknown. Mirrors
    ``navig_text.design_tokens._resolve_space_root``: a space's canonical id comes
    from its manifest, not its folder name, so the name is normalised first. A path
    is accepted when it points at a directory carrying a ``.navig/`` — cron jobs and
    the Telegram intake pass absolute paths, and a registry lookup must not be the
    only way in.
    """
    candidate = Path(space).expanduser()
    if (
        candidate.is_absolute()
        and candidate.is_dir()
        and (candidate / ".navig").is_dir()
    ):
        return candidate

    # A space NAME is looked up in navig's space registry — imported only now, so an
    # absolute path works without navig (standalone navig-cabinet) and only a name needs it.
    from navig.spaces.contracts import normalize_space_name
    from navig.spaces.resolver import discover_space_paths

    cfg = (discover_space_paths() or {}).get(normalize_space_name(space))
    root = str(getattr(cfg, "path", "") or "") if cfg is not None else ""
    if not root:
        raise ValueError(f"space not found: {space!r} (see `navig space list`)")
    return Path(root)


@dataclass(frozen=True)
class PaperworkPaths:
    """Every path this plugin may write to, derived from one space root."""

    space_root: Path

    @property
    def base(self) -> Path:
        return self.space_root / ".navig" / "paperwork"

    @property
    def plan_csv(self) -> Path:
        return self.base / "plan.csv"

    @property
    def handoff_jsonl(self) -> Path:
        return self.base / "handoff.jsonl"

    @property
    def handoff_md(self) -> Path:
        return self.base / "handoff.md"

    @property
    def documents_jsonl(self) -> Path:
        return self.base / "documents.jsonl"

    @property
    def index_md(self) -> Path:
        return self.base / "index.md"

    @property
    def receipts_dir(self) -> Path:
        return self.base / "receipts"

    @property
    def trash_root(self) -> Path:
        return self.base / ".trash"

    def scan_jsonl(self, stamp: str) -> Path:
        return self.base / f"scan-{stamp}.jsonl"

    def archived_plan(self, stamp: str) -> Path:
        return self.base / f"plan-{stamp}.csv"

    def receipt(self, stamp: str) -> Path:
        return self.receipts_dir / f"apply-{stamp}.jsonl"

    def contains(self, path: Path) -> bool:
        """Is *path* inside this space? The guard every write passes through."""
        try:
            path.resolve().relative_to(self.space_root.resolve())
        except (ValueError, OSError):
            return False
        return True


def paths_for(space: str) -> PaperworkPaths:
    return PaperworkPaths(resolve_space_root(space))
