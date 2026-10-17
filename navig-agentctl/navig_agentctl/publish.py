"""Fan a run change out to the live UI (SSE) and the notification router.

Best-effort and isolated: a broken notifier never fails an ingest, and chatty
activity events are throttled so a busy session does not flood the event stream.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

#: Minimum seconds between SSE pushes for the same run when only activity changed.
SSE_THROTTLE_S = 2.0
_last_push: dict[str, tuple[float, str]] = {}

_NOTIFY = {
    "done": ("agent_done", "normal"),
    "turn_done": ("agent_done", "normal"),
    "failed": ("agent_failed", "high"),
    "needs_input": ("agent_needs_input", "high"),
    "stalled": ("agent_stalled", "high"),
}


def _fmt_elapsed(seconds: int | None) -> str:
    if not seconds:
        return ""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s" if m else f"{s}s"


def describe(run: dict[str, Any], kind: str, took_s: int | None = None) -> tuple[str, str]:
    """Title and body for a transition. Built only from already-redacted fields."""
    name = run["label"] or run["kind"]
    where = " · ".join(x for x in (run["project"], run["node"]) if x)
    if kind == "turn_done":
        title = f"Claude finished its turn — {run['project'] or name}"
        parts = [f"took {_fmt_elapsed(took_s)}" if took_s else ""]
    elif kind == "done":
        title = f"{name} finished"
        parts = [f"exit {run['exit_code']}" if run["exit_code"] is not None else "",
                 "verified" if run["verified"] else "", _fmt_elapsed(run["elapsed_s"])]
    elif kind == "failed":
        title = f"{name} failed" if run["state"] == "failed" else f"{name} stopped unexpectedly"
        parts = [f"exit {run['exit_code']}" if run["exit_code"] is not None else "",
                 run["detail"] if run["state"] == "interrupted" else "", _fmt_elapsed(run["elapsed_s"])]
    elif kind == "needs_input":
        title, parts = f"{name} needs you", [run["detail"]]
    else:  # stalled
        title, parts = f"{name} may be stuck", [run["detail"]]
    body = " · ".join(p for p in (where, *parts) if p)
    return title, body


async def publish(app, result: dict[str, Any]) -> None:
    run, transition = result.get("run"), result.get("transition")
    if not run or result.get("duplicate"):
        return
    gateway = app.get("gateway") if app is not None else None
    now = time.monotonic()
    last_t, last_sig = _last_push.get(run["run_id"], (0.0, ""))
    sig = f"{run['state']}|{run['phase']}|{run['stages_done']}"
    if transition or sig != last_sig or now - last_t >= SSE_THROTTLE_S:
        _last_push[run["run_id"]] = (now, sig)
        if len(_last_push) > 2000:
            _last_push.clear()
        events = getattr(gateway, "system_events", None)
        if events is not None:
            try:
                await events.emit("agent_run", run)
            except Exception:  # noqa: BLE001 — telemetry must not break ingest
                logger.debug("agent_run SSE emit failed", exc_info=True)
    if not transition:
        return
    spec = _NOTIFY.get(transition["kind"])
    if spec is None:
        return
    title, body = describe(run, transition["kind"], transition.get("took_s"))
    try:
        from navig.notify.router import dispatch

        await dispatch(
            spec[0], title, body, priority=spec[1],
            data={"run_id": run["run_id"], "kind": run["kind"], "state": run["state"]},
        )
    except Exception:  # noqa: BLE001
        logger.warning("agent monitor notify failed", exc_info=True)
