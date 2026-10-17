"""Agent Control Center deck routes — the read-only Observer API.

AUTHORITATIVE CONTRACT FIXTURE: ``plugins/navig-agentctl/tests/fixtures/agents-contract.json``.
The OS Control Center app and tools/forge/navig-dev both validate against it —
one fixture, two consumers; a shape change updates the fixture in the same
commit as the route change. (navig-dev's drift-alarm walks
``plugins/navig-agentctl/**`` for it — the routes are a PLUGIN, not core.)

Gated on the ``agentctl`` module being enabled. Reads are GET-only; the
writes are annotation/ledger only (no process control):

  GET    /api/deck/agents/sessions            → {sessions: [...], generated_at}
  GET    /api/deck/agents/worktrees?project=  → {worktrees: [...], conflicts: [...]}
  GET    /api/deck/agents/tasks               → {tasks: [...], generated_at}
  POST   /api/deck/agents/heartbeat           → {ok, session_id}   annotation-only
  GET    /api/deck/agents/bindings[?all=1]    → {bindings: [...], generated_at}
  POST   /api/deck/agents/bindings            → {ok, binding}      one claim per task
  DELETE /api/deck/agents/bindings/{task_ref} → {ok, released}     ledger keeps history

The Agent Monitor run ledger (events in, runs/summary out) is documented and
implemented in ``monitor_routes.py``; it mounts from ``register`` below.

Heartbeat semantics (contract): payload carries ONLY annotation fields
(``session_id`` + optional ``project_root, worktree, agent_kind, editor,
owner, last_activity_at``); unknown/daemon fields in the body are ignored,
never written. Daemon-derived state always wins — the route cannot change
``state``/``pid``/``reason`` of a session the process table owns.

Bindings semantics (contract): ``task_ref`` = ``board:<id>`` | ``work:<slug>``
(one active claim per ref — rebind MOVES the owner); ``session_id`` follows the
heartbeat contract. Releasing stamps ``released_at``; the ledger row survives
for history (``?all=1``). A claim may precede its session's spawn — liveness
gates are the display layer's job, not this ledger's.

Future local-only mutations (launch/assign/pause/terminate) need BOTH guards
(is_local_request in the brain AND the OS server's refuseRemoteManagement
list); heartbeat and bindings are annotation, not control, so they ride the
normal deck auth.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

try:
    from aiohttp import web
except ImportError:  # pragma: no cover — gateway always has aiohttp
    web = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_BASE = "/api/deck/agents"


def _json(data: dict[str, Any], status: int = 200):
    return web.json_response(data, status=status)


def _resolve_root(request) -> Path | None:
    """Project root from ?project= (a path), else the primary checkout of cwd.

    Typed paths follow the operator: a path the user typed goes through
    resolve_user_path — never Path.cwd() guessing for user input.
    """
    raw = (request.query.get("project") or "").strip()
    if not raw:
        from navig.platform.paths import invocation_cwd

        raw = str(invocation_cwd())
    try:
        from navig.platform.paths import resolve_user_path

        root = resolve_user_path(raw)
    except Exception:  # noqa: BLE001
        root = Path(raw)
    if not root.exists():
        return None
    return root


async def handle_sessions(request):
    """Live sessions (procs-derived) merged with the registry ledger."""
    from navig_agentctl.registry import get_agentctl_store
    from navig_agentctl.snapshots import record_snapshot, session_snapshot

    try:
        # to_thread: psutil process scans take seconds — run on the loop would
        # freeze EVERY other gateway request (deck, telegram) behind this one.
        rows = await asyncio.to_thread(session_snapshot)
    except Exception as exc:  # noqa: BLE001 — observer must never 500 the app
        logger.warning("agentctl sessions snapshot failed: %r", exc)
        rows = []
    try:
        def _ingest() -> dict:
            store = get_agentctl_store()
            record_snapshot(rows, store)
            store.mark_stale()
            return {r["session_id"]: r for r in store.list_sessions()}

        ledger = await asyncio.to_thread(_ingest)
    except Exception as exc:  # noqa: BLE001
        logger.debug("agentctl registry unavailable: %r", exc)
        ledger = {}

    # Live rows win; registry-only rows surface as their last known state
    # (stale included — the recovery breadcrumb Phase 3 needs).
    seen = {r["session_id"] for r in rows}
    merged = list(rows)
    for sid, r in ledger.items():
        if sid not in seen:
            merged.append(
                {
                    "session_id": sid,
                    "pid": r.get("pid"),
                    "project_root": r.get("project_root") or "",
                    "project": r.get("project") or "",
                    "worktree": r.get("worktree") or "",
                    "agent_kind": r.get("agent_kind") or "",
                    "editor": r.get("editor") or "",
                    "state": r.get("state") or "stale",
                    "reason": r.get("reason") or "not seen in the latest snapshot",
                    "last_activity_at": r.get("last_activity_at"),
                    "owner": r.get("owner") or "",
                    "mem_mb": 0.0,
                }
            )
    return _json({"sessions": merged, "generated_at": _now()})


async def handle_worktrees(request):
    """Worktrees of one repo + the pairwise conflict radar."""
    from navig_agentctl.snapshots import conflict_pairs, worktree_snapshot

    root = _resolve_root(request)
    if root is None:
        return _json({"error": "project path not found"}, status=404)
    try:
        # to_thread: N worktrees × 3 git subprocesses measured 36-150s on the
        # live box — on the loop it froze the whole gateway (reproduced: a
        # 14ms route took 147s while this ran).
        wts = await asyncio.to_thread(worktree_snapshot, root)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agentctl worktree snapshot failed: %r", exc)
        return _json({"error": f"worktree snapshot failed: {exc}"}, status=500)
    # Conflict radar is opt-in: pairwise merge-tree is O(n²) git calls — a
    # 31-worktree repo is ~450 pairs and the scan runs off-loop (to_thread),
    # so the rest of the gateway stays live while it grinds. The CLI mirrors
    # the opt-in (`--conflicts` is a flag, never the default).
    with_conflicts = (request.query.get("conflicts") or "0").lower() in ("1", "true", "yes")
    pairs = await asyncio.to_thread(conflict_pairs, root) if with_conflicts else []
    return _json(
        {"project": str(root), "worktrees": wts, "conflicts": pairs, "generated_at": _now()}
    )


async def handle_tasks(request):
    """Unified task read-model: board cards + work items."""
    from navig_agentctl.snapshots import task_snapshot

    try:
        tasks = await asyncio.to_thread(task_snapshot)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agentctl task snapshot failed: %r", exc)
        return _json({"error": f"task snapshot failed: {exc}"}, status=500)
    return _json({"tasks": tasks, "generated_at": _now()})


def _reject(msg: str):
    """400s must be VISIBLE: a silent rejection is a client that retries
    forever with a payload nobody ever sees fail (the forge client's
    404-standdown made exactly this class of drift undetectable before)."""
    logger.warning("agentctl request rejected: %s", msg)
    return _json({"error": msg}, status=400)


def _session_id_error(value: object) -> str | None:
    """The shared session_id contract (heartbeat + bindings): non-empty
    string ≤200 chars, no control characters. Returns the rejection reason
    or None when valid."""
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        return "session_id is required (non-empty string, ≤200 chars)"
    if any(ord(c) < 32 for c in value):
        return "session_id contains control characters"
    return None


async def handle_heartbeat(request):
    """POST — annotation-only session heartbeat (the ONE write in Phase 1/2a).

    Adds editor context the daemon cannot derive (workspace, editor, roster
    owner) to the registry. Never changes state/pid/reason — see the module
    docstring for the contract. Validation is fail-closed: a malformed body
    is 400, never a partial write.
    """
    from navig_agentctl.registry import get_agentctl_store

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — non-JSON body
        return _reject("body must be a JSON object")
    if not isinstance(payload, dict):
        return _reject("body must be a JSON object")

    sid = payload.get("session_id")
    bad = _session_id_error(sid)
    if bad:
        return _reject(bad)

    annotations: dict[str, str] = {}
    for key in ("project_root", "worktree", "agent_kind", "editor", "owner"):
        val = payload.get(key)
        if val is None:
            annotations[key] = ""
            continue
        if not isinstance(val, str) or len(val) > 1024:
            return _reject(f"{key} must be a string of at most 1024 chars")
        annotations[key] = val
    last_activity = payload.get("last_activity_at")
    if last_activity is not None and (
        not isinstance(last_activity, str) or len(last_activity) > 64
    ):
        return _reject("last_activity_at must be a string of at most 64 chars")

    try:
        store = get_agentctl_store()
        store.annotate_session(
            {"session_id": sid.strip(), **annotations, "last_activity_at": last_activity}
        )
    except Exception as exc:  # noqa: BLE001 — store down is a 500, not a 200
        logger.warning("agentctl heartbeat store failed: %r", exc)
        return _json({"error": f"heartbeat store failed: {exc}"}, status=500)
    return _json({"ok": True, "session_id": sid.strip()})


_TASK_REF_RE = re.compile(r"(?:board|work):[A-Za-z0-9._-]{1,120}\Z")


async def handle_bindings_list(request):
    """GET — active task↔session bindings (?all=1 includes the released ledger)."""
    from navig_agentctl.registry import get_agentctl_store

    all_rows = (request.query.get("all") or "0").lower() in ("1", "true", "yes")
    try:
        store = get_agentctl_store()
        rows = await asyncio.to_thread(lambda: store.bindings(active_only=not all_rows))
    except Exception as exc:  # noqa: BLE001 — store down is a 500, not an empty list
        logger.warning("agentctl bindings list failed: %r", exc)
        return _json({"error": f"bindings store failed: {exc}"}, status=500)
    return _json({"bindings": rows, "generated_at": _now()})


async def handle_binding_create(request):
    """POST — claim a task for a session (annotation, not control).

    Rebinding moves the owner (store upsert): one active binding per task
    ref, ever — the no-duplicate-execution ledger. The session need not be
    live: a claim may precede the spawn; the display layers gate on liveness.
    """
    from navig_agentctl.registry import get_agentctl_store

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — non-JSON body
        return _reject("body must be a JSON object")
    if not isinstance(payload, dict):
        return _reject("body must be a JSON object")

    ref = payload.get("task_ref")
    if not isinstance(ref, str) or not _TASK_REF_RE.match(ref):
        return _reject("task_ref must look like 'board:<id>' or 'work:<slug>'")
    sid = payload.get("session_id")
    bad = _session_id_error(sid)
    if bad:
        return _reject(bad)
    note = payload.get("note") or ""
    if not isinstance(note, str) or len(note) > 300:
        return _reject("note must be a string of at most 300 chars")

    try:
        store = get_agentctl_store()
        await asyncio.to_thread(store.bind, ref, sid.strip(), note=note)
        binding = await asyncio.to_thread(store.binding_for, ref)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agentctl bind failed: %r", exc)
        return _json({"error": f"bindings store failed: {exc}"}, status=500)
    return _json({"ok": True, "binding": binding})


async def handle_binding_release(request):
    """DELETE — release a task claim (records released_at; the ledger keeps it)."""
    from navig_agentctl.registry import get_agentctl_store

    ref = request.match_info.get("task_ref", "")
    if not _TASK_REF_RE.match(ref):
        return _reject("task_ref must look like 'board:<id>' or 'work:<slug>'")
    try:
        store = get_agentctl_store()

        def _release() -> bool:
            was_active = store.binding_for(ref) is not None
            store.release(ref)
            return was_active

        released = await asyncio.to_thread(_release)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agentctl unbind failed: %r", exc)
        return _json({"error": f"bindings store failed: {exc}"}, status=500)
    return _json({"ok": True, "released": released})


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def register(app: "web.Application") -> None:
    from navig.modules.gate import requires_module

    g = requires_module("agentctl")
    app.router.add_get(f"{_BASE}/sessions", g(handle_sessions))
    app.router.add_get(f"{_BASE}/worktrees", g(handle_worktrees))
    app.router.add_get(f"{_BASE}/tasks", g(handle_tasks))
    app.router.add_post(f"{_BASE}/heartbeat", g(handle_heartbeat))
    app.router.add_get(f"{_BASE}/bindings", g(handle_bindings_list))
    app.router.add_post(f"{_BASE}/bindings", g(handle_binding_create))
    app.router.add_delete(f"{_BASE}/bindings/{{task_ref}}", g(handle_binding_release))
    from navig_agentctl import monitor_routes

    monitor_routes.register(app, g)  # Agent Monitor: events + runs
    logger.debug(
        "navig-agentctl: Observer + heartbeat + bindings routes mounted under %s", _BASE
    )
