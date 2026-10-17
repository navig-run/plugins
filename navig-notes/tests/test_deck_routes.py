"""The notes routes: this computer only, the sync round trip, no GET side effects."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("aiohttp")

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from conftest import dock_note  # noqa: E402
from navig_notes import deck_routes as R  # noqa: E402


class _App:
    """Mount the routes without the module gate (the gate is core's, tested there)."""

    def __init__(self):
        self.app = web.Application()
        import navig.modules.gate as gate

        self._orig = gate.requires_module
        gate.requires_module = lambda _id: (lambda h: h)
        try:
            R.register(self.app)
        finally:
            gate.requires_module = self._orig


def session(steps):
    async def _go():
        async with TestClient(TestServer(_App().app)) as client:
            return await steps(client)

    return asyncio.run(_go())


TUNNEL = {"CF-Ray": "8abc", "CF-Connecting-IP": "203.0.113.9"}


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/deck/notes"), ("POST", "/api/deck/notes/sync"), ("GET", "/api/deck/notes/search?q=x"),
    ("GET", "/api/deck/notes/status"), ("GET", "/api/deck/notes/n1"), ("POST", "/api/deck/notes"),
])
def test_every_route_refuses_tunnelled_callers(db_path, method, path):
    async def steps(client):
        resp = await client.request(method, path, headers=TUNNEL, json={})
        return resp.status, await resp.json()

    status, body = session(steps)
    assert status == 403 and body["code"] == "local_only"


def test_sync_round_trip_and_reads(db_path):
    async def steps(client):
        r = await client.post("/api/deck/notes/sync", json={"since": 0, "share": "local", "notes": [dock_note("a"), dock_note("b", body="retry path")], "deleted": []})
        first = (await r.json())["data"]
        r = await client.post("/api/deck/notes", json={"body": "from the agent", "scope": "global", "origin": "agent"})
        made = (await r.json())["data"]
        r = await client.post("/api/deck/notes/sync", json={"since": first["cursor"], "notes": [], "deleted": [{"id": "a", "rev": 1}]})
        second = (await r.json())["data"]
        listing = (await (await client.get("/api/deck/notes")).json())["data"]["notes"]
        hits = (await (await client.get("/api/deck/notes/search?q=retry")).json())["data"]["notes"]
        status = (await (await client.get("/api/deck/notes/status")).json())["data"]
        one = await client.get(f"/api/deck/notes/{made['id']}")
        missing = await client.get("/api/deck/notes/nope")
        return first, made, second, listing, hits, status, (await one.json())["data"], missing.status

    first, made, second, listing, hits, status, one, missing = session(steps)
    assert first["results"]["added"] == 2 and first["changes"] == []
    assert made["origin"] == "agent" and made["scope"] == "global"
    assert [c["id"] for c in second["changes"]] == [made["id"]], "only what changed elsewhere comes back"
    assert second["results"]["deleted"] == 1
    assert {n["id"] for n in listing} == {"b", made["id"]}
    assert [n["id"] for n in hits] == ["b"]
    assert status["notes"] == 2 and status["last_sync"]["pushed"] == 0 and status["deleted"] == 1
    assert one["body"] == "from the agent" and missing == 404


def test_sync_refuses_oversized_batches_and_bad_shapes(db_path):
    async def steps(client):
        big = await client.post("/api/deck/notes/sync", json={"notes": [dock_note(f"n{i}") for i in range(R.MAX_SYNC_NOTES + 1)]})
        bad = await client.post("/api/deck/notes/sync", json={"notes": "nope"})
        empty = await client.post("/api/deck/notes", json={"body": "  "})
        return big.status, (await big.json())["code"], bad.status, empty.status

    big, code, bad, empty = session(steps)
    assert (big, code) == (413, "too_many")
    assert bad == 400 and empty == 400


def test_gets_change_nothing(db_path, store):
    store.sync([dock_note("a")], [])
    before = store.cursor()

    async def steps(client):
        for p in ("/api/deck/notes", "/api/deck/notes/search?q=hello", "/api/deck/notes/status", "/api/deck/notes/a"):
            assert (await client.get(p)).status == 200

    session(steps)
    assert store.cursor() == before
