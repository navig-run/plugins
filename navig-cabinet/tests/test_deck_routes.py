"""The Cabinet deck routes: this computer only, metadata only, locked means locked."""

from __future__ import annotations

import asyncio
import os
from datetime import date, timedelta

import pytest

pytest.importorskip("aiohttp")

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from navig_cabinet import deck_routes as R  # noqa: E402
from navig_cabinet import keys  # noqa: E402
from navig_cabinet.ingest import add_path  # noqa: E402
from navig_cabinet.store import Cabinet  # noqa: E402

ROUTES = [
    ("GET", "/api/deck/cabinet/status", R.handle_status),
    ("GET", "/api/deck/cabinet/items", R.handle_items),
    ("POST", "/api/deck/cabinet/open", R.handle_open),
    ("POST", "/api/deck/cabinet/add", R.handle_add),
    ("POST", "/api/deck/cabinet/unlock", R.handle_unlock),
    ("POST", "/api/deck/cabinet/lock", R.handle_lock),
]


def _app() -> web.Application:
    app = web.Application()
    for method, path, handler in ROUTES:
        app.router.add_route(method, path, R._local_only(handler))
    return app


def call(method: str, path: str, **kw) -> tuple[int, dict]:
    async def _go():
        async with TestClient(TestServer(_app())) as client:
            resp = await client.request(method, path, **kw)
            return resp.status, await resp.json()

    return asyncio.run(_go())


@pytest.fixture(autouse=True)
def _no_session():
    R._session.update(master=None, root=None, last=0.0)
    yield
    R._session.update(master=None, root=None, last=0.0)


@pytest.fixture
def filled(cab, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    lab = src / "lab-results.pdf"
    lab.write_bytes(os.urandom(50))
    item = add_path(cab, lab, read_text=False, category="medical",
                    expires=(date.today() + timedelta(days=20)).isoformat()).item
    cab.update(item.id, notes="n")
    # give it searchable text the way OCR would
    cab.close()
    with Cabinet.open(cab.root) as c:
        c.update(item.id, title="Blood test March")
    return item


@pytest.mark.parametrize("method,path,_h", ROUTES)
def test_every_route_refuses_traffic_that_came_through_the_tunnel(method, path, _h):
    status, body = call(method, path, headers={"CF-Ray": "8abc", "CF-Connecting-IP": "203.0.113.9"})
    assert status == 403 and body["code"] == "local_only"


def test_status_before_a_cabinet_exists():
    status, body = call("GET", "/api/deck/cabinet/status")
    assert status == 200 and body["data"]["exists"] is False


def test_status_and_items_carry_metadata_never_the_text(filled):
    status, body = call("GET", "/api/deck/cabinet/status")
    data = body["data"]
    assert data["unlocked"] and data["items"] == 1 and data["by_category"] == {"medical": 1}
    assert [e["id"] for e in data["expiring"]] == [filled.id]
    status, body = call("GET", "/api/deck/cabinet/items", params={"q": "blood march"})
    [row] = body["data"]["items"]
    assert row["title"] == "Blood test March" and "text" not in row
    assert row["days_to_expiry"] == 20
    status, body = call("GET", "/api/deck/cabinet/items", params={"category": "identity"})
    assert body["data"]["items"] == []


def test_add_takes_absolute_paths_and_reports_duplicates(tmp_path, root):
    f = tmp_path / "scan.png"
    f.write_bytes(os.urandom(64))
    status, body = call("POST", "/api/deck/cabinet/add", json={"paths": [str(f)]})
    assert status == 200 and len(body["data"]["added"]) == 1, body
    status, body = call("POST", "/api/deck/cabinet/add", json={"paths": [str(f)]})
    assert len(body["data"]["duplicates"]) == 1
    assert call("POST", "/api/deck/cabinet/add", json={"paths": ["relative.pdf"]})[0] == 400
    assert call("POST", "/api/deck/cabinet/add", json={})[0] == 400
    assert f.exists(), "the original is never touched"


def test_open_decrypts_locally_and_launches(filled, monkeypatch):
    launched = []
    import navig_cabinet.commands.cabinet as C

    monkeypatch.setattr(C, "_launch", lambda p: launched.append(p))
    status, body = call("POST", "/api/deck/cabinet/open", json={"id": filled.id})
    assert status == 200 and body["data"]["opened"], body
    assert launched and launched[0].parent.name == ".open"
    assert call("POST", "/api/deck/cabinet/open", json={})[0] == 400


def test_a_passphrase_cabinet_is_locked_until_unlocked(filled, root):
    with Cabinet.open(root) as c:
        keys.set_passphrase(root, c.master_key, "pw")
    status, body = call("GET", "/api/deck/cabinet/status")
    assert body["data"]["unlocked"] is False and "items" not in body["data"]
    assert call("GET", "/api/deck/cabinet/items")[0] == 423
    status, body = call("POST", "/api/deck/cabinet/unlock", json={"passphrase": "wrong"})
    assert status == 403 and body["code"] == "wrong_passphrase"
    assert call("POST", "/api/deck/cabinet/unlock", json={"passphrase": "pw"})[0] == 200
    assert len(call("GET", "/api/deck/cabinet/items")[1]["data"]["items"]) == 1
    assert call("POST", "/api/deck/cabinet/lock")[0] == 200
    assert call("GET", "/api/deck/cabinet/items")[0] == 423


def test_an_idle_session_expires(filled, root, monkeypatch):
    with Cabinet.open(root) as c:
        keys.set_passphrase(root, c.master_key, "pw")
    call("POST", "/api/deck/cabinet/unlock", json={"passphrase": "pw"})
    monkeypatch.setattr(R, "SESSION_IDLE_SECONDS", -1)
    assert call("GET", "/api/deck/cabinet/items")[0] == 423
