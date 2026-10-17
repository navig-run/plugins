"""Agent Monitor routes — ingestion validation, read API, log confinement, notifications."""

from __future__ import annotations

import os

import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

BASE = "/api/deck/agents"


@pytest.fixture()
async def client(monkeypatch, tmp_path):
    from navig_agentctl import logs, registry as reg

    monkeypatch.setattr(reg, "_default_db_path", lambda: tmp_path / "agentctl.db")
    monkeypatch.setattr(logs, "log_root", lambda: tmp_path / "logs" / "agent-runs")
    (tmp_path / "logs" / "agent-runs").mkdir(parents=True)

    from navig.modules.registry import register_module
    from navig_agentctl.plugin import _agentctl_module_def

    register_module(_agentctl_module_def())

    sent: list[tuple] = []

    async def fake_dispatch(type_key, title, body="", **kw):
        sent.append((type_key, title, body, kw.get("priority")))
        return {}

    import navig.notify.router as router

    monkeypatch.setattr(router, "dispatch", fake_dispatch)

    app = web.Application()
    from navig_agentctl import deck_routes

    deck_routes.register(app)
    cli = TestClient(TestServer(app))
    await cli.start_server()
    cli.sent = sent
    cli.tmp = tmp_path
    yield cli
    await cli.close()


async def post(client, body):
    return await client.post(f"{BASE}/events", json=body)


async def test_start_and_failed_end_notify_once(client):
    r = await post(client, {"run_id": "t1", "type": "start", "kind": "bash", "label": "nightly build", "event_id": "a"})
    assert r.status == 200
    r = await post(client, {"run_id": "t1", "type": "end", "state": "failed", "exit_code": 3, "event_id": "b"})
    assert (await r.json())["results"][0]["state"] == "failed"
    await post(client, {"run_id": "t1", "type": "end", "state": "failed", "exit_code": 3, "event_id": "b"})  # replay
    assert [s[0] for s in client.sent] == ["agent_failed"]
    assert "exit 3" in client.sent[0][2] and client.sent[0][3] == "high"


async def test_runs_list_detail_summary_and_ack(client):
    await post(client, {"run_id": "r-ok", "type": "start", "kind": "navig", "label": "ok job"})
    await post(client, {"run_id": "r-ok", "type": "end", "state": "succeeded", "exit_code": 0, "verified": True})
    await post(client, {"run_id": "r-bad", "type": "start", "kind": "navig", "label": "bad job"})
    await post(client, {"run_id": "r-bad", "type": "end", "state": "failed", "exit_code": 1})

    runs = (await (await client.get(f"{BASE}/runs")).json())["runs"]
    assert {r["run_id"] for r in runs} == {"r-ok", "r-bad"} and all(r["node"] for r in runs)

    summary = (await (await client.get(f"{BASE}/runs/summary")).json())["summary"]
    assert summary["attention"] == "failed" and summary["failed_unacked"] == 1

    detail = await (await client.get(f"{BASE}/runs/r-bad")).json()
    assert detail["run"]["exit_code"] == 1 and detail["events"]

    assert (await (await client.post(f"{BASE}/runs/r-bad/ack")).json())["acked"] is True
    assert (await (await client.get(f"{BASE}/runs/summary")).json())["summary"]["attention"] == "idle"
    assert (await client.get(f"{BASE}/runs/nope")).status == 404


@pytest.mark.parametrize(
    "body",
    [
        {"type": "start"},  # no run_id
        {"run_id": "x", "type": "bogus"},
        {"run_id": "x" * 400, "type": "start"},
        {"run_id": "x", "type": "start", "pid": "12"},
        {"run_id": "x", "type": "start", "verified": "yes"},
        {"run_id": "x", "type": "stage", "stage": "compile"},
        ["not", "an", "object"],
        {"events": []},
    ],
)
async def test_malformed_events_are_rejected_not_partially_written(client, body):
    r = await post(client, body)
    assert r.status == 400
    assert (await (await client.get(f"{BASE}/runs")).json())["runs"] == []


async def test_a_batch_is_all_or_nothing(client):
    r = await post(client, {"events": [{"run_id": "good", "type": "start"}, {"run_id": "", "type": "start"}]})
    assert r.status == 400
    assert (await (await client.get(f"{BASE}/runs")).json())["runs"] == []


async def test_stage_events_report_declared_progress(client):
    await post(client, {"run_id": "l", "type": "start", "kind": "land", "stages_planned": ["rebase", "gate", "push"]})
    await post(client, {"run_id": "l", "type": "stage", "stage": {"name": "rebase", "state": "done"}})
    run = (await (await client.get(f"{BASE}/runs/l")).json())["run"]
    assert (run["stages_done"], run["stages_total"]) == (1, 3)


async def test_secrets_never_come_back_out(client):
    await post(client, {"run_id": "s", "type": "start", "kind": "bash",
                        "command": "deploy --token abcdef0123456789 --password hunter2",
                        "detail": "Authorization: Bearer abcdef0123456789"})
    run = (await (await client.get(f"{BASE}/runs/s")).json())["run"]
    blob = str(run)
    assert "abcdef0123456789" not in blob and "hunter2" not in blob


async def test_log_tail_is_confined_to_the_log_directory(client):
    root = client.tmp / "logs" / "agent-runs"
    good = root / "r1.log"
    good.write_text("line one\nTOKEN=supersecretvalue123\nline three\n", encoding="utf-8")
    outside = client.tmp / "outside.txt"
    outside.write_text("do not serve me", encoding="utf-8")

    await post(client, {"run_id": "r1", "type": "start", "log_path": str(good)})
    await post(client, {"run_id": "evil", "type": "start", "log_path": str(outside)})
    await post(client, {"run_id": "trav", "type": "start", "log_path": str(root / ".." / "outside.txt")})

    log = (await (await client.get(f"{BASE}/runs/r1?log=1")).json())["log"]
    assert log[0] == "line one" and "supersecretvalue123" not in "".join(log)
    for rid in ("evil", "trav"):
        body = await (await client.get(f"{BASE}/runs/{rid}?log=1")).json()
        assert body["log"] == [] and body["run"]["has_log"] is False


async def test_needs_input_and_stalled_notify(client):
    await post(client, {"run_id": "n", "type": "start", "kind": "session", "label": "Claude Code"})
    await post(client, {"run_id": "n", "type": "needs_input", "detail": "Claude needs your permission"})
    assert client.sent[-1][0] == "agent_needs_input"

    from navig_agentctl import monitor_routes
    from navig_agentctl.registry import get_agentctl_store

    await post(client, {"run_id": "w", "type": "start", "kind": "bash", "stall_after_s": 1, "pid": os.getpid()})
    store = get_agentctl_store()
    from datetime import datetime, timedelta, timezone

    stalled = store.sweep_runs(now=datetime.now(timezone.utc) + timedelta(seconds=30))
    assert [s["kind"] for s in stalled] == ["stalled"]
    from navig_agentctl.publish import publish

    for item in stalled:
        await publish(None, {"run": item["run"], "transition": {"kind": item["kind"]}})
    assert client.sent[-1][0] == "agent_stalled"
    assert monitor_routes.SWEEP_INTERVAL_S > 0


async def test_api_shapes_match_the_authoritative_contract_fixture(client):
    """One fixture, two consumers: the live Run / summary / envelope keys must equal the fixture's."""
    import json
    from pathlib import Path

    fixture = json.loads((Path(__file__).parent / "fixtures" / "agents-contract.json").read_text(encoding="utf-8"))
    await post(client, {"run_id": "c1", "type": "start", "kind": "land", "stages_planned": ["gate"]})
    live = (await (await client.get(f"{BASE}/runs")).json())["runs"][0]
    for row in fixture["runs"]:
        assert set(row) == set(live), f"Run keys drifted: {set(row) ^ set(live)}"
    summary = (await (await client.get(f"{BASE}/runs/summary")).json())["summary"]
    assert set(summary) == set(fixture["runs_summary"])
    detail = await (await client.get(f"{BASE}/runs/c1?log=1")).json()
    assert set(detail) == {"run", "events", "log"}
    assert set(fixture["_phase3_events"]["payload"]) <= {
        "event_id", "run_id", "type", "stage", "kind", "label", "state", "exit_code", "detail"}


def test_tests_never_touch_the_operators_spool(_isolate_navig_locations):
    """Regression: the reaper a test app starts must drain a TEMP spool, not ~/.navig's."""
    from pathlib import Path

    from navig_agentctl import spool

    assert spool.spool_path().is_relative_to(_isolate_navig_locations)
    assert not spool.spool_path().is_relative_to(Path.home() / ".navig")
