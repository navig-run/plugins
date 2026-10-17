"""Deck routes — the Observer reads are GET-only, module-gated, and MUST
yield the event loop.

Contract: sessions/worktrees/tasks are GET-only (mutations: only the
Phase-2a annotation POST /heartbeat); every non-GET on the read routes 405s.
The snapshot work runs in worker threads — a sync scan on the loop froze
the entire gateway (reproduced: a 14ms route took 147s behind one
worktrees request).
"""

from __future__ import annotations

import asyncio

import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402


@pytest.fixture()
async def client(monkeypatch, tmp_path):
    """A gateway app with the Observer routes + an isolated registry."""
    from navig_agentctl import registry as reg

    monkeypatch.setattr(reg, "_default_db_path", lambda: tmp_path / "agentctl.db")

    # The gate refuses unknown module ids — register agentctl like the plugin does.
    from navig.modules.registry import register_module
    from navig_agentctl.plugin import _agentctl_module_def

    register_module(_agentctl_module_def())

    app = web.Application()
    from navig_agentctl import deck_routes

    deck_routes.register(app)
    cli = TestClient(TestServer(app))
    await cli.start_server()
    yield cli
    await cli.close()


async def test_sessions_route_lists_sessions(client):
    resp = await client.get("/api/deck/agents/sessions")
    assert resp.status == 200
    body = await resp.json()
    assert isinstance(body["sessions"], list)
    assert body["generated_at"]


async def test_tasks_route_lists_tasks(client):
    resp = await client.get("/api/deck/agents/tasks")
    assert resp.status == 200
    body = await resp.json()
    assert isinstance(body["tasks"], list)


async def test_worktrees_route_404s_on_missing_project(client):
    resp = await client.get("/api/deck/agents/worktrees?project=/definitely/not/here-xyz")
    assert resp.status == 404


async def test_no_mutation_verbs_exist(client):
    """The read paths stay read-only: every non-GET on the Phase-1 routes
    must 405, and the heartbeat's GET must too (it is POST-only)."""
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        resp = await client.request(method, "/api/deck/agents/sessions")
        assert resp.status == 405, f"{method} unexpectedly allowed on sessions"
    for method in ("PUT", "PATCH", "DELETE"):
        resp = await client.request(method, "/api/deck/agents/tasks")
        assert resp.status == 405, f"{method} unexpectedly allowed on tasks"
    resp = await client.get("/api/deck/agents/heartbeat")
    assert resp.status == 405, "heartbeat must be POST-only"


async def test_heartbeat_registers_and_surfaces_in_sessions(client):
    """A heartbeat-created session appears in GET sessions as state 'unknown'
    (the daemon has not classified it), carrying the annotation."""
    resp = await client.post(
        "/api/deck/agents/heartbeat",
        json={
            "session_id": "hb-0001",
            "project_root": "E:\\proj\\demo",
            "worktree": "demo-wt",
            "editor": "vscode",
            "owner": "session-42",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"ok": True, "session_id": "hb-0001"}

    resp = await client.get("/api/deck/agents/sessions")
    rows = (await resp.json())["sessions"]
    mine = next(r for r in rows if r["session_id"] == "hb-0001")
    assert mine["state"] == "unknown"
    assert mine["project_root"] == "E:\\proj\\demo"
    assert mine["worktree"] == "demo-wt"
    assert mine["owner"] == "session-42"


async def test_heartbeat_never_overwrites_daemon_truth(client):
    """state/pid/reason are daemon-owned: a malicious or buggy body carrying
    them is IGNORED, and a later heartbeat cannot flip a classified state."""
    from navig_agentctl.registry import get_agentctl_store

    store = get_agentctl_store()
    store.upsert_session(
        {
            "session_id": "hb-0002",
            "pid": 4242,
            "state": "active",
            "reason": "tool running (bun)",
            "project": "demo",
        }
    )
    resp = await client.post(
        "/api/deck/agents/heartbeat",
        json={
            "session_id": "hb-0002",
            "state": "idle",          # daemon-owned — must be ignored
            "pid": 1,                 # daemon-owned — must be ignored
            "reason": "spoofed",      # daemon-owned — must be ignored
            "owner": "roster-x",
        },
    )
    assert resp.status == 200

    row = store.get_session("hb-0002")
    assert row["state"] == "active"
    assert row["pid"] == 4242
    assert row["reason"] == "tool running (bun)"
    assert row["owner"] == "roster-x"  # annotation DID land


async def test_heartbeat_rejects_malformed_bodies(client):
    """Fail closed: bad bodies are 400, never a partial write."""
    resp = await client.post("/api/deck/agents/heartbeat", data="not json")
    assert resp.status == 400

    resp = await client.post("/api/deck/agents/heartbeat", json={})
    assert resp.status == 400  # session_id required

    resp = await client.post(
        "/api/deck/agents/heartbeat", json={"session_id": "x", "worktree": 123}
    )
    assert resp.status == 400  # wrong type

    resp = await client.post(
        "/api/deck/agents/heartbeat",
        json={"session_id": "bad\x00sid"},
    )
    assert resp.status == 400  # control characters rejected


def test_routes_are_module_gated():
    """Every mounted handler must carry the requires_module tag.

    aiohttp's add_get registers GET + HEAD against the same handler, so the
    count is over UNIQUE handlers, not route objects.
    """
    from aiohttp import web as _web

    from navig_agentctl import deck_routes

    app = _web.Application()
    deck_routes.register(app)
    unique = {
        id(r.handler): r.handler
        for r in app.router.routes()
        if hasattr(r, "handler") and r.handler
    }
    gated = [h for h in unique.values() if getattr(h, "__navig_required_module__", None) == "agentctl"]
    assert len(gated) == 12, (
        f"expected 12 gated handlers (4 reads + heartbeat + bindings list/create "
        f"+ release + Agent Monitor events/runs/summary/detail/ack), found {len(gated)}"
    )


async def test_worktrees_handler_yields_the_event_loop(client, monkeypatch):
    """The snapshot MUST run off-loop. Deterministic proof, no timing budget:

    the blocking snapshot fires an event from its own thread. If the handler
    awaited properly (to_thread), the loop is free and this coroutine resumes
    while the handler is still pending. If the snapshot ran ON the loop (the
    pre-fix bug, measured live at 147s of gateway freeze), this coroutine
    cannot resume until the handler has already completed — so the pending
    assertion fails on old code and passes on new.
    """
    import threading
    import time

    from navig_agentctl import snapshots

    sync_started = threading.Event()

    def blocking_snapshot(root):
        sync_started.set()
        time.sleep(0.6)
        return []

    monkeypatch.setattr(snapshots, "worktree_snapshot", blocking_snapshot)

    handler_task = asyncio.create_task(
        client.get("/api/deck/agents/worktrees?project=.")
    )
    started = await asyncio.to_thread(sync_started.wait, 5.0)
    assert started, "the snapshot never started"
    assert not handler_task.done(), (
        "handler blocked the event loop: the worktrees snapshot ran "
        "synchronously inside the async handler (every other gateway "
        "request queues behind it)"
    )
    resp = await handler_task
    assert resp.status == 200
    body = await resp.json()
    assert body["worktrees"] == []
    assert body["conflicts"] == []


async def test_heartbeat_rejection_is_logged_not_silent(client, caplog):
    """A rejected heartbeat must warn — a silent 400 lets a client retry a bad
    payload forever with nobody able to see it fail (rock-2 residual)."""
    import logging

    with caplog.at_level(logging.WARNING, logger="navig_agentctl.deck_routes"):
        resp = await client.post("/api/deck/agents/heartbeat", json={})
    assert resp.status == 400
    rejected = [r for r in caplog.records if "request rejected" in r.message]
    assert rejected, "a 400 heartbeat must emit a warning naming the rejection"  # message text generalized to any agentctl 400


async def test_binding_lifecycle_create_list_release(client):
    """The no-duplicate-execution ledger end to end: one active claim per
    task ref, rebind MOVES the owner, release stamps history."""
    # create
    resp = await client.post(
        "/api/deck/agents/bindings",
        json={"task_ref": "board:abc-123", "session_id": "sess-9", "note": "claimed"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["binding"]["task_ref"] == "board:abc-123"
    assert body["binding"]["session_id"] == "sess-9"
    assert body["binding"]["released_at"] is None

    # rebind moves the owner (no second row — duplicate execution impossible)
    resp = await client.post(
        "/api/deck/agents/bindings",
        json={"task_ref": "board:abc-123", "session_id": "sess-10"},
    )
    assert resp.status == 200
    resp = await client.get("/api/deck/agents/bindings")
    rows = (await resp.json())["bindings"]
    assert len(rows) == 1
    assert rows[0]["session_id"] == "sess-10"

    # release — hidden from active, kept in the ledger
    resp = await client.delete("/api/deck/agents/bindings/board:abc-123")
    assert resp.status == 200
    assert (await resp.json()) == {"ok": True, "released": True}
    resp = await client.get("/api/deck/agents/bindings")
    assert (await resp.json())["bindings"] == []
    resp = await client.get("/api/deck/agents/bindings?all=1")
    rows = (await resp.json())["bindings"]
    assert len(rows) == 1 and rows[0]["released_at"]

    # releasing twice reports honestly
    resp = await client.delete("/api/deck/agents/bindings/board:abc-123")
    assert (await resp.json())["released"] is False


async def test_bindings_fail_closed_on_bad_input(client):
    """Malformed refs/ids are 400 and never reach the store."""
    for bad in ("", "card:abc", "board:", "board:has space", "board:a/b", 123):
        resp = await client.post(
            "/api/deck/agents/bindings", json={"task_ref": bad, "session_id": "s"}
        )
        assert resp.status == 400, f"task_ref {bad!r} should be rejected"

    resp = await client.post(
        "/api/deck/agents/bindings",
        json={"task_ref": "board:ok", "session_id": ""},
    )
    assert resp.status == 400

    resp = await client.post(
        "/api/deck/agents/bindings",
        json={"task_ref": "board:ok", "session_id": "s", "note": "x" * 301},
    )
    assert resp.status == 400

    resp = await client.delete("/api/deck/agents/bindings/not-a-ref")
    assert resp.status == 400

    # nothing leaked into the store
    resp = await client.get("/api/deck/agents/bindings?all=1")
    assert (await resp.json())["bindings"] == []


async def test_bindings_store_failure_is_500_not_empty(client, monkeypatch):
    """A down store must not masquerade as 'no claims' (the phantom-empty
    class): 500 with an error body."""
    from navig_agentctl import registry as reg

    def boom(*a, **k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(reg.AgentCtlStore, "bindings", boom)
    resp = await client.get("/api/deck/agents/bindings")
    assert resp.status == 500
    body = await resp.json()
    assert "bindings store failed" in body["error"]
