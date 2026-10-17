"""The Grid Pigeon's moments → recording markers (for ``navig obs clips``).

The pigeon page (SCHEMA's ``overlays/pigeon/engine.js``) reports each headline
moment — hacker mode, a celebration, a lag rant — and every line he speaks, by
``sendBeacon`` to the gateway route ``POST /obs/pigeon/moment``. Here each one
is checked and stamped on the RECORDING clock:

* a headline moment → one contract-7 marker (``pigeon-<kind>``, kind
  ``character``) in the session's ``markers.jsonl`` — the contract is frozen and
  admits no extra fields, so nothing else goes there;
* a spoken line → ``pigeon.jsonl`` beside it (tOffset, group, text, ms, lang):
  the captions a clip burns in.

The session opens itself: the first moment of a recording creates a contract-7
session anchored at ``now - outputDuration`` (OBS's own recording clock), and
``pigeon.json`` keeps what the contract cannot hold (the record directory). A
moment while OBS is not recording is dropped — there is no clock to put it on.

Trust: the route is outside the deck's auth, and the gateway can be reached
through a tunnel, so a moment must carry the TOKEN minted here
(``~/.navig/obs-show/moment-token``; SCHEMA's installer writes it into the page).
Kinds are a fixed list, text is capped, and a burst is rate-limited.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from navig_obs.show.settings import state_dir

CONTRACT_VERSION = 1
TOKEN_FILE = "moment-token"
SIDE_LINES = "pigeon.jsonl"
SIDE_META = "pigeon.json"

# What the page may report. Anything else is refused by name.
HEADLINE = frozenset({
    "hacker", "celebrate", "windfall", "cuckoo", "mission", "passport", "join",
    "warning", "dance", "moonwalk", "malfunction", "screenpeck",
    "anger", "uptime", "lag", "live",
})
LINE = "line"
LANGS = ("en", "fr", "ru")
MAX_TEXT = 300
MAX_PER_MINUTE = 40
SESSION_MATCH_S = 10.0  # a session within this of the recording's start is the same one
_GROUP = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class MomentRejected(ValueError):
    """The report is not one the page is allowed to make (logged, never raised to OBS)."""


def token_path() -> Path:
    return state_dir() / TOKEN_FILE


def ensure_token(path: Path | None = None) -> str:
    """The shared secret, minted once. Only this machine's user can read it."""
    p = path or token_path()
    try:
        tok = p.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{16,128}", tok):
            return tok
    except OSError:
        pass
    p.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    p.write_text(tok + "\n", encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return tok


def validate(payload: Any, token: str) -> dict[str, Any]:
    """A clean moment, or MomentRejected naming why."""
    if not isinstance(payload, dict):
        raise MomentRejected("not an object")
    got = str(payload.get("token") or "")
    if not token or not hmac.compare_digest(got, token):
        raise MomentRejected("bad token")
    kind = str(payload.get("kind") or "")
    if kind != LINE and kind not in HEADLINE:
        raise MomentRejected(f"unknown kind {kind[:40]!r}")
    lang = str(payload.get("lang") or "en")
    out: dict[str, Any] = {"kind": kind, "lang": lang if lang in LANGS else "en"}
    if kind == LINE:
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise MomentRejected("a line needs its text")
        out["text"] = " ".join(text.split())[:MAX_TEXT]
        group = str(payload.get("group") or "")
        out["group"] = group if _GROUP.match(group) else "line"
        try:
            ms = int(payload.get("ms") or 0)
        except (TypeError, ValueError):
            ms = 0
        out["ms"] = max(500, min(ms or 3000, 60000))
    return out


@dataclass
class RateLimit:
    per_minute: int = MAX_PER_MINUTE
    _times: list[float] = field(default_factory=list)

    def allow(self, now: float) -> bool:
        self._times = [t for t in self._times if now - t < 60.0]
        if len(self._times) >= self.per_minute:
            return False
        self._times.append(now)
        return True


def sessions_root() -> Path:
    base = os.environ.get("NAVIG_OBS_SESSIONS")
    return Path(base).expanduser() if base else Path.home() / ".navig" / "obs-sessions"


def _infer_lang(collection: str) -> str:
    m = re.search(r"\((en|fr|ru)\)", collection or "")
    return m.group(1) if m else "en"


@dataclass
class Recorder:
    """Writes moments into the session of the recording that is running."""

    root: Path = field(default_factory=sessions_root)
    _current: Path | None = None
    _current_start: int = 0

    def find_session(self, start_epoch: int) -> Path | None:
        """An open session (no endEpoch) whose clock matches this recording."""
        if self._current and abs(self._current_start - start_epoch) <= SESSION_MATCH_S * 1000:
            if (self._current / "session.json").is_file():
                return self._current
        if not self.root.is_dir():
            return None
        best: tuple[float, Path, int] | None = None
        for d in self.root.iterdir():
            f = d / "session.json"
            if not f.is_file():
                continue
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if rec.get("endEpoch") is not None:
                continue
            gap = abs(int(rec.get("startEpoch") or 0) - start_epoch)
            if gap <= SESSION_MATCH_S * 1000 and (best is None or gap < best[0]):
                best = (gap, d, int(rec["startEpoch"]))
        if best:
            self._current, self._current_start = best[1], best[2]
            return best[1]
        return None

    def open_session(self, start_epoch: int, ctx: dict[str, Any]) -> Path:
        stamp = datetime.fromtimestamp(start_epoch / 1000).strftime("%Y%m%d-%H%M%S")
        sid = f"{stamp}-{secrets.token_hex(2)}"
        d = self.root / sid
        d.mkdir(parents=True, exist_ok=False)
        collection = str(ctx.get("collection") or "")
        record = {
            "contractVersion": CONTRACT_VERSION,
            "sessionId": sid,
            "startEpoch": int(start_epoch),
            "scene": str(ctx.get("scene") or ""),
            "collection": collection,
            "lang": _infer_lang(collection),
            "portrait": bool(ctx.get("portrait")),
            "recordingPath": None,
            "markersFile": "markers.jsonl",
            "endEpoch": None,
            "showId": None,
        }
        _write_json(d / "session.json", record)
        (d / "markers.jsonl").write_text("", encoding="utf-8")
        _write_json(d / SIDE_META, {
            "auto": True,
            "by": "pigeon moments",
            "recordDirectory": ctx.get("recordDirectory"),
        })
        self._current, self._current_start = d, int(start_epoch)
        return d

    def record(
        self, moment: dict[str, Any], status: dict[str, Any], ctx: dict[str, Any],
        now_ms: int | None = None,
    ) -> dict[str, Any] | None:
        """Stamp one moment; None when OBS is not recording (no clock)."""
        if not status.get("outputActive"):
            return None
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        t_offset = max(0.0, int(status.get("outputDuration") or 0) / 1000.0)
        start = now - int(t_offset * 1000)
        d = self.find_session(start) or self.open_session(start, ctx)
        if moment["kind"] == LINE:
            row = {
                "tOffset": round(t_offset, 3), "wallClock": now, "group": moment["group"],
                "text": moment["text"], "ms": moment["ms"], "lang": moment["lang"],
            }
            _append(d / SIDE_LINES, row)
            return {"session": str(d), "line": row}
        line = {
            "contractVersion": CONTRACT_VERSION,
            "tOffset": round(t_offset, 3),
            "wallClock": now,
            "label": f"pigeon-{moment['kind']}",
            "kind": "character",
        }
        _append(d / "markers.jsonl", line)
        return {"session": str(d), "marker": line}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _append(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
