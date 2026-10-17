"""The chat library routes: this computer only, no GET side effects, single-use downloads."""

from __future__ import annotations

import asyncio
import io
import time
import zipfile

import pytest

pytest.importorskip("aiohttp")

from aiohttp import FormData, web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from navig_chats import deck_routes as R  # noqa: E402


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


def call(method: str, path: str, *, raw: bool = False, **kw):
    async def _go():
        async with TestClient(TestServer(_App().app)) as client:
            resp = await client.request(method, path, **kw)
            if raw:
                return resp.status, await resp.read(), dict(resp.headers)
            return resp.status, await resp.json()

    return asyncio.run(_go())


def session(steps):
    """Several requests against ONE server (the import thread and tokens live in-process)."""
    async def _go():
        async with TestClient(TestServer(_App().app)) as client:
            return await steps(client)

    return asyncio.run(_go())


TUNNEL = {"CF-Ray": "8abc", "CF-Connecting-IP": "203.0.113.9"}


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/deck/chats"), ("GET", "/api/deck/chats/search?q=x"), ("POST", "/api/deck/chats/capture"),
    ("GET", "/api/deck/chats/chatgpt:x"), ("POST", "/api/deck/chats/export"), ("GET", "/api/deck/chats/overview"),
])
def test_every_route_refuses_tunnelled_traffic(db_path, method, path):
    status, body = call(method, path, headers=TUNNEL)
    assert status == 403 and body["code"] == "local_only"


def _capture_body(**over):
    body = {
        "provider": "chatgpt", "native_id": "live-1", "url": "https://chatgpt.com/c/live-1", "title": "Live one",
        "completeness": "rendered",
        "messages": [{"id": "a", "role": "user", "text": "deploy the worker"},
                     {"id": "b", "role": "assistant", "text": "```bash\nwrangler deploy\n```", "has_code": True}],
    }
    body.update(over)
    return body


def test_capture_visit_search_and_open(db_path):
    async def steps(c):
        r = await c.post("/api/deck/chats/visit", json={"native_id": "live-1"})
        assert (await r.json())["data"]["saved"] is False
        folder = (await (await c.post("/api/deck/chats/folders", json={"name": "Ops"})).json())["data"]
        r = await c.post("/api/deck/chats/capture", json=_capture_body(folder_id=folder["id"]))
        data = (await r.json())["data"]
        assert data["status"] == "added" and data["conversation"]["folders"] == [folder["id"]]
        r = await c.post("/api/deck/chats/visit", json={"native_id": "live-1"})
        assert (await r.json())["data"]["saved"] is True
        r = await c.get("/api/deck/chats/search", params={"q": "wrangler", "code": "1"})
        [hit] = (await r.json())["data"]["results"]
        assert hit["hits"][0]["message_id"] == "b"
        r = await c.get("/api/deck/chats/chatgpt:live-1/messages")
        assert [m["id"] for m in (await r.json())["data"]["messages"]] == ["a", "b"]
        r = await c.get("/api/deck/chats/overview")
        ov = (await r.json())["data"]
        assert ov["stats"]["conversations"] == 1 and ov["folders"][0]["count"] == 1

    session(steps)


@pytest.mark.parametrize("bad", [
    {"native_id": "", "url": ""},
    {"messages": "nope"},
    {"messages": [{"role": "wizard", "text": "x"}]},
    {"messages": [1]},
])
def test_capture_rejects_bad_payloads(db_path, bad):
    status, body = call("POST", "/api/deck/chats/capture", json=_capture_body(**bad))
    assert status == 400 and body["code"] == "bad_capture"


def test_capture_without_messages_is_a_link_only_bookmark(db_path):
    status, body = call("POST", "/api/deck/chats/capture", json=_capture_body(messages=[], native_id="", url="https://chatgpt.com/share/xyz"))
    assert status == 200 and body["data"]["conversation"]["completeness"] == "url_only"


def test_patch_bulk_and_delete(db_path):
    async def steps(c):
        await c.post("/api/deck/chats/capture", json=_capture_body())
        r = await c.patch("/api/deck/chats/chatgpt:live-1", json={"alias": "Deploy notes", "tags": ["Ops Work"], "evil": 1})
        conv = (await r.json())["data"]
        assert conv["display_title"] == "Deploy notes" and conv["tags"] == ["ops-work"]
        r = await c.post("/api/deck/chats/bulk", json={"ids": ["chatgpt:live-1"], "action": "archive"})
        assert (await r.json())["data"]["changed"] == 1
        r = await c.post("/api/deck/chats/bulk", json={"ids": ["chatgpt:live-1"], "action": "explode"})
        assert r.status == 400
        r = await c.delete("/api/deck/chats/chatgpt:live-1")
        assert r.status == 200
        r = await c.get("/api/deck/chats/chatgpt:live-1")
        assert r.status == 404

    session(steps)


def test_search_with_only_exclusions_is_a_clear_empty_answer(db_path):
    status, body = call("GET", "/api/deck/chats/search", params={"q": "-python"})
    assert status == 200 and body["data"]["results"] == [] and "nothing to search" in body["data"]["note"]


def test_export_token_is_single_use(db_path):
    async def steps(c):
        await c.post("/api/deck/chats/capture", json=_capture_body())
        r = await c.post("/api/deck/chats/export", json={"ids": ["chatgpt:live-1"], "format": "md"})
        url = (await r.json())["data"]["url"]
        r = await c.get(url)
        assert r.status == 200 and "attachment" in r.headers["Content-Disposition"]
        assert "wrangler deploy" in await r.text()
        r = await c.get(url)
        assert r.status == 410
        r = await c.post("/api/deck/chats/export", json={"collection": "all", "format": "zip"})
        url = (await r.json())["data"]["url"]
        r = await c.get(url)
        assert r.status == 200 and r.headers["Content-Type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(await r.read())) as zf:
            assert "manifest.json" in zf.namelist()

    session(steps)


def test_upload_import_runs_in_the_background(db_path, sample_export):
    async def steps(c):
        form = FormData()
        form.add_field("file", sample_export.read_bytes(), filename="chatgpt-export.zip", content_type="application/zip")
        r = await c.post("/api/deck/chats/import", data=form)
        assert r.status == 202
        job = (await r.json())["data"]["job"]
        deadline = time.monotonic() + 20
        while True:
            r = await c.get(f"/api/deck/chats/import/{job}")
            data = (await r.json())["data"]
            if data["status"] != "running" or time.monotonic() > deadline:
                break
            await asyncio.sleep(0.05)
        assert data["status"] == "done" and data["added"] == 3
        r = await c.get("/api/deck/chats", params={"sort": "oldest"})
        assert (await r.json())["data"]["total"] == 3

    session(steps)
    assert not list((db_path.parent / "data" / "chats-imports").glob("upload-*"))  # temp upload removed
