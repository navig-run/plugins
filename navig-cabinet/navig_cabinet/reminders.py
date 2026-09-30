"""Expiry reminders that arrive by themselves — passports, ID cards, insurance.

The gateway's notify scheduler calls :func:`tick` every ~45 s (a soft import, only when
the ``cabinet`` module is enabled). Once a day, after ``cabinet.reminders.hour`` (default
9), it warns about documents that expire in 90, 30 or 7 days, and once more when one
has expired — each threshold at most once per document and expiry date, so renewing a
passport (a new ``--expires``) starts its reminders over.

**What a reminder reveals.** The daemon must not need the passphrase, and a Telegram
message must not carry a medical document's title. So the scheduler never opens the
cabinet: it reads a small **reminder index** (``reminders.bin``) that the cabinet writes
whenever its contents change — only ``(id, category, expires)`` per dated item, sealed
with a key bound to this machine. The message reads "an identity document expires in 28
days (`navig cabinet show 3fa9`)". In a passphrase-locked cabinet that index is the one
thing readable without the passphrase, on this machine; ``navig config set
cabinet.reminders.enabled false`` turns reminders off and deletes it.

Delivery goes through the notify router as type ``reminder`` (deck bell + Telegram by
default, per Settings → Notifications). A threshold is marked sent only when the router
accepted the dispatch, so a failed delivery is retried on the next tick instead of
becoming a phantom success.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import keys

from navig_sdk.host import command_name  # noqa: E402

# The command a user types here: `navig cabinet` inside navig, `navig-cabinet` on its own.
CMD = command_name("cabinet", standalone="navig-cabinet")

logger = logging.getLogger(__name__)

INDEX_FILE = "reminders.bin"
STATE_FILE = "reminders_state.json"
THRESHOLDS = (90, 30, 7)          # days before expiry; plus one "expired" notice
RETRY_SECONDS = 3600               # after a failed delivery
_AAD = b"navig-cabinet/reminders/v1"
_SALT = b"navig-cabinet/reminders/salt/v1"  # fixed: the material is already machine-unique

CATEGORY_WORDS = {
    "identity": "an identity document", "medical": "a medical document",
    "insurance": "an insurance document", "finance": "a financial document",
    "housing": "a housing document", "legal": "a legal document",
    "education": "an education document", "vehicle": "a vehicle document",
    "photos": "a photo", "recordings": "a recording", "other": "a document",
}


# ── config ──────────────────────────────────────────────────────────────────


def _config() -> dict[str, Any]:
    """``cabinet.reminders`` from navig's config.yaml — navig's loader inside navig, the same
    file read directly on its own (read-only: an unreadable file reads as defaults)."""
    try:
        from navig.config import get_config_manager
    except ImportError:
        from navig_sdk import files
        from navig_sdk.host import config_dir

        data = files.safe_load_yaml(config_dir() / "config.yaml") or {}
    else:
        try:
            data = get_config_manager().global_config or {}
        except Exception:  # noqa: BLE001 — unreadable config: defaults
            return {}
    node = data.get("cabinet", {}) if isinstance(data, dict) else {}
    node = node.get("reminders", {}) if isinstance(node, dict) else {}
    return node if isinstance(node, dict) else {}


def enabled() -> bool:
    from navig_sdk.host import coerce_bool

    return coerce_bool(_config().get("enabled"), default=True)


def reminder_hour() -> int:
    from navig_cabinet._core import coerce_int

    hour = coerce_int(_config().get("hour"), default=9)
    return hour if 0 <= hour <= 23 else 9


# ── the index (written by the cabinet, read by the scheduler) ──────────────


def _key() -> bytes:
    material, _source = keys.machine_material()
    return keys.derive(material + b"/reminders", _SALT, 2**12)


def write_index(root: Path, items) -> None:
    """Replace the index with the dated, active items. Never raises."""
    try:
        if not enabled():
            (root / INDEX_FILE).unlink(missing_ok=True)
            return
        rows = [{"id": it.id, "category": it.category, "expires": it.expires}
                for it in items if it.expires and it.state == "active"]
        nonce = os.urandom(12)
        blob = nonce + AESGCM(_key()).encrypt(nonce, json.dumps(rows).encode("utf-8"), _AAD)
        tmp = root / (INDEX_FILE + ".tmp")
        tmp.write_bytes(blob)
        keys._owner_only(tmp)
        os.replace(tmp, root / INDEX_FILE)
    except Exception:  # noqa: BLE001 — a reminder index must never break the cabinet
        logger.debug("cabinet: reminder index not written", exc_info=True)


def read_index(root: Path) -> list[dict]:
    p = root / INDEX_FILE
    if not p.is_file():
        return []
    blob = p.read_bytes()
    try:
        body = AESGCM(_key()).decrypt(blob[:12], blob[12:], _AAD)
    except Exception:  # noqa: BLE001 — another machine's index, or damage: nothing to remind
        logger.debug("cabinet: reminder index unreadable on this machine")
        return []
    return json.loads(body.decode("utf-8"))


# ── deciding what is due ────────────────────────────────────────────────────


def _load_state(root: Path) -> dict:
    try:
        return json.loads((root / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(root: Path, state: dict) -> None:
    tmp = root / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, root / STATE_FILE)


def due(rows: list[dict], sent: dict, today: date) -> list[tuple[dict, int, str]]:
    """``(row, days_left, mark)`` for every reminder not yet sent. ``mark`` is the key to
    record once delivered: ``<id>|<expires>|<threshold>``, or ``…|expired``."""
    out = []
    for r in rows:
        try:
            days_left = (date.fromisoformat(r["expires"]) - today).days
        except (KeyError, TypeError, ValueError):
            continue
        if days_left < 0:
            level = "expired"
        else:
            hits = [t for t in THRESHOLDS if days_left <= t]
            if not hits:
                continue
            level = str(min(hits))  # the tightest threshold crossed is the one to announce
        mark = f"{r['id']}|{r['expires']}|{level}"
        if mark not in sent:
            out.append((r, days_left, mark))
    return sorted(out, key=lambda x: x[1])


def message(batch: list[tuple[dict, int, str]]) -> tuple[str, str]:
    lines = []
    for r, days_left, _mark in batch:
        what = CATEGORY_WORDS.get(r.get("category") or "", "a document")
        if days_left < 0:
            when = f"expired {-days_left} day(s) ago"
        elif days_left == 0:
            when = "expires today"
        else:
            when = f"expires in {days_left} day(s) ({r['expires']})"
        lines.append(f"• {what[0].upper() + what[1:]} {when} — `{CMD} show {r['id']}`")
    n = len(batch)
    title = f"🗂 {n} document{'s' if n != 1 else ''} in your cabinet need{'' if n != 1 else 's'} renewing"
    return title, "\n".join(lines)


async def _dispatch(title: str, body: str) -> bool:
    try:
        from navig.notify.router import get_notification_router

        await get_notification_router().dispatch("reminder", title, body, priority="normal",
                                                 data={"source": "cabinet"})
        return True
    except Exception:  # noqa: BLE001 — not delivered: leave it unmarked so it retries
        logger.debug("cabinet: reminder dispatch failed", exc_info=True)
        return False


async def check(root: Path, *, now: datetime | None = None, force: bool = False,
                dispatch=None) -> dict:
    """Send what is due. Returns ``{sent, pending, skipped}`` — used by tick and the CLI."""
    now = now or datetime.now()
    state = _load_state(root)
    today = now.date()
    if not force:
        if now.hour < reminder_hour() or state.get("last_day") == today.isoformat():
            return {"sent": 0, "pending": 0, "skipped": "not due yet today"}
        last_try = state.get("last_attempt")
        if last_try:
            try:
                if (now - datetime.fromisoformat(last_try)).total_seconds() < RETRY_SECONDS:
                    return {"sent": 0, "pending": 0, "skipped": "retrying later"}
            except ValueError:
                pass
    rows = read_index(root)
    sent = dict(state.get("sent") or {})
    batch = due(rows, sent, today)
    if not batch:
        state["last_day"] = today.isoformat()
        _save_state(root, state)
        return {"sent": 0, "pending": 0, "skipped": None}
    title, body = message(batch)
    ok = await (dispatch or _dispatch)(title, body)
    if not ok:
        # Not a phantom success: nothing is marked sent. But not a 45-second retry storm
        # against a channel that is down either — try again in an hour.
        state["last_attempt"] = now.isoformat(timespec="seconds")
        _save_state(root, state)
        return {"sent": 0, "pending": len(batch), "skipped": "delivery failed — will retry"}
    for _r, _d, mark in batch:
        sent[mark] = today.isoformat()
    live = {f"{r['id']}|{r['expires']}" for r in rows}
    state["sent"] = {k: v for k, v in sent.items() if k.rsplit("|", 1)[0] in live}
    state["last_day"] = today.isoformat()
    state.pop("last_attempt", None)
    _save_state(root, state)
    return {"sent": len(batch), "pending": 0, "skipped": None}


async def tick(gateway=None) -> None:
    """The scheduler's entry point. Cheap when nothing is due; never raises."""
    try:
        if not enabled():
            return
        from .store import default_root

        root = default_root()
        if not (root / INDEX_FILE).is_file():
            return
        await check(root)
    except Exception:  # noqa: BLE001 — never break the scheduler loop
        logger.debug("cabinet: reminder tick failed", exc_info=True)
