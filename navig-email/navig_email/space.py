"""Resolve the mailroom space and own every path the email plugin writes under it.

Same rule as ``navig_cabinet.paperwork.space``: **nothing this plugin writes may land outside
the space it was pointed at.** Human-facing files live in ``<space>/mailroom/``
(rules, ledgers, reports, the corpus); machine state in ``<space>/.navig/email/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

MAILROOM_DIR = "mailroom"


def resolve_space_root(space: str) -> Path:
    """A space NAME (registry) or an absolute path to a directory carrying ``.navig/``.

    Raises ``ValueError`` when neither resolves.
    """
    candidate = Path(space).expanduser()
    if (
        candidate.is_absolute()
        and candidate.is_dir()
        and (candidate / ".navig").is_dir()
    ):
        return candidate

    from navig.spaces.contracts import normalize_space_name
    from navig.spaces.resolver import discover_space_paths

    cfg = (discover_space_paths() or {}).get(normalize_space_name(space))
    root = str(getattr(cfg, "path", "") or "") if cfg is not None else ""
    if not root:
        raise ValueError(f"space not found: {space!r} (see `navig space list`)")
    return Path(root)


def read_space_config(space_root: Path) -> dict:
    path = space_root / ".navig" / "config.yaml"
    if not path.exists():
        return {}
    try:
        from navig_sdk.files import safe_load_yaml  # navig's reader, or its twin standalone

        data = safe_load_yaml(path)
    except Exception:  # noqa: BLE001 — a broken config means "defaults"
        return {}
    return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class MailroomPaths:
    """Every path the email plugin may write to, derived from one space root."""

    space_root: Path

    # ── human-facing ──
    @property
    def base(self) -> Path:
        return self.space_root / MAILROOM_DIR

    @property
    def rules_yaml(self) -> Path:
        rel = (self.config.get("mailroom") or {}).get(
            "rules"
        ) or f"{MAILROOM_DIR}/rules.yaml"
        return self.space_root / rel

    @property
    def ledger_dir(self) -> Path:
        return self.base / "ledger"

    def ledger(self, name: str) -> Path:
        return self.ledger_dir / f"{_safe(name)}.jsonl"

    @property
    def reports_dir(self) -> Path:
        return self.base / "reports"

    def corpus_dir(self, name: str) -> Path:
        return self.base / _safe(name)

    @property
    def style_path(self) -> Path | None:
        rel = (self.config.get("mailroom") or {}).get("style")
        return (self.space_root / rel) if rel else None

    # ── machine state ──
    @property
    def state_dir(self) -> Path:
        return self.space_root / ".navig" / "email"

    @property
    def state_json(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def config(self) -> dict:
        return read_space_config(self.space_root)

    @property
    def account(self) -> str:
        return str((self.config.get("mailroom") or {}).get("account") or "")

    @property
    def ai(self) -> dict:
        """``mailroom.ai`` — ``{model, allow_cloud}``. The model pin is what keeps the
        mailroom's own calls on a local model regardless of the global mode router."""
        block = (self.config.get("mailroom") or {}).get("ai")
        return block if isinstance(block, dict) else {}

    @property
    def ai_model(self) -> str:
        return str(self.ai.get("model") or "").strip()

    @property
    def ai_allow_cloud(self) -> bool:
        return bool(self.ai.get("allow_cloud"))

    def contains(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.space_root.resolve())
        except (ValueError, OSError):
            return False
        return True


def _safe(name: str) -> str:
    keep = "".join(
        c if (c.isalnum() or c in "-_") else "-" for c in (name or "").strip().lower()
    )
    return keep.strip("-") or "default"


def paths_for(space: str) -> MailroomPaths:
    return MailroomPaths(resolve_space_root(space))
