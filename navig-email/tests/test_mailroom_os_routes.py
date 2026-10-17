"""Mailroom 2.0 deck routes (NAVIG OS): outbox, pause, policies, briefing, accounts, history.

The guards matter more than the happy path: approving needs the hash the person saw, "send now"
needs ``confirm: true``, enabling a sending policy needs ``confirm: true``, unknown actions are
refused, and list responses never carry a message body.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("aiohttp")

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from fake_gmail import FakeGmail  # noqa: E402
from navig_email import outbox as ob  # noqa: E402
from navig_email.deck_routes import mailroom as M  # noqa: E402
from navig_email.space import MailroomPaths  # noqa: E402


def _app() -> web.Application:
    app = web.Application()
    M.register(app, lambda h: h)
    return app


def call(method: str, path: str, **kw) -> tuple[int, dict]:
    async def _go():
        async with TestClient(TestServer(_app())) as client:
            resp = await client.request(method, path, **kw)
            return resp.status, await resp.json()

    return asyncio.run(_go())


@pytest.fixture
def paths(tmp_path) -> MailroomPaths:
    root = tmp_path / "space"
    (root / ".navig").mkdir(parents=True)
    (root / "mailroom").mkdir(parents=True)
    (root / ".navig" / "config.yaml").write_text("mailroom: {}\n", encoding="utf-8")
    (root / "mailroom" / "accounts.yaml").write_text(
        "accounts:\n  - address: box@gmail.com\n    aliases:\n      - { address: hello@studio.test }\n", encoding="utf-8")
    return MailroomPaths(root)


@pytest.fixture
def box(monkeypatch):
    fake = FakeGmail("box@gmail.com")

    async def factory(_a=None):
        return fake

    monkeypatch.setattr(ob, "CONNECTOR_FACTORY", factory)
    monkeypatch.setattr(ob, "ACCOUNT_CHECK", lambda _a: True)
    return fake


def _pending(paths):
    return ob.submit(ob.create(account="box@gmail.com", to="ann@client.test", subject="Re: hi",
                               body="SECRET BODY TEXT here.", paths=paths)["job_id"])


def test_outbox_list_has_no_bodies_and_counts(paths, box):
    job = _pending(paths)
    status, data = call("GET", "/api/deck/email/mailroom/outbox")
    assert status == 200 and data["data"]["jobs"][0]["job_id"] == job["job_id"]
    assert "SECRET" not in str(data) and data["data"]["counts"]["PENDING_APPROVAL"] == 1
    status, one = call("GET", f"/api/deck/email/mailroom/outbox/job?id={job['job_id']}")
    assert one["data"]["job"]["body"].startswith("SECRET") and one["data"]["events"]


def test_approve_requires_the_seen_hash_then_cancel(paths, box):
    job = _pending(paths)
    status, data = call("POST", "/api/deck/email/mailroom/outbox/act",
                        json={"action": "approve", "job": job["job_id"], "hash": "x"})
    assert status == 409
    status, data = call("POST", "/api/deck/email/mailroom/outbox/act",
                        json={"action": "approve", "job": job["job_id"], "hash": job["content_hash"], "in": "30m"})
    assert status == 200 and data["data"]["job"]["state"] == "SCHEDULED"
    status, data = call("POST", "/api/deck/email/mailroom/outbox/act", json={"action": "cancel", "job": job["job_id"]})
    assert data["data"]["job"]["state"] == "CANCELLED" and box.sent_messages == []


def test_send_now_needs_confirm(paths, box):
    job = _pending(paths)
    ob.approve(job["job_id"], content_hash_seen=job["content_hash"])
    status, _ = call("POST", "/api/deck/email/mailroom/outbox/act", json={"action": "send_now", "job": job["job_id"]})
    assert status == 400 and box.sent_messages == []
    status, data = call("POST", "/api/deck/email/mailroom/outbox/act",
                        json={"action": "send_now", "job": job["job_id"], "confirm": True})
    assert status == 200 and data["data"]["job"]["state"] == "SENT" and len(box.sent_messages) == 1


def test_unknown_action_and_unknown_state_are_refused(paths, box):
    assert call("POST", "/api/deck/email/mailroom/outbox/act", json={"action": "rm -rf", "job": "x"})[0] == 400
    assert call("GET", "/api/deck/email/mailroom/outbox?state=EVERYTHING")[0] == 400


def test_edit_resets_approval(paths, box):
    job = _pending(paths)
    ob.approve(job["job_id"], content_hash_seen=job["content_hash"])
    status, data = call("POST", "/api/deck/email/mailroom/outbox/act",
                        json={"action": "edit", "job": job["job_id"], "body": "A new text entirely."})
    assert status == 200 and data["data"]["job"]["state"] == "PENDING_APPROVAL"


def test_pause_roundtrip(paths, box):
    assert call("POST", "/api/deck/email/mailroom/pause", json={"level": "all"})[1]["data"]["pause"] == "all"
    assert call("GET", "/api/deck/email/mailroom/pause")[1]["data"]["pause"] == "all"
    assert call("POST", "/api/deck/email/mailroom/pause", json={"level": "nope"})[0] == 400
    assert call("POST", "/api/deck/email/mailroom/pause", json={"level": ""})[1]["data"]["pause"] == ""


def test_enabling_a_sending_policy_needs_confirm(paths, box):
    status, data = call("POST", "/api/deck/email/mailroom/policies/act",
                        json={"action": "enable", "id": "business-ack", "account": "box@gmail.com"})
    assert status == 400 and "confirm" in data["error"]
    status, data = call("POST", "/api/deck/email/mailroom/policies/act",
                        json={"action": "enable", "id": "business-ack", "account": "box@gmail.com", "confirm": True})
    assert status == 200 and data["data"]["policy"]["enabled"] is True
    status, data = call("GET", "/api/deck/email/mailroom/policies")
    assert any(p["id"] == "business-ack" and p["enabled"] for p in data["data"]["policies"])
    status, data = call("POST", "/api/deck/email/mailroom/policies/act", json={"action": "disable", "id": "business-ack"})
    assert data["data"]["policy"]["enabled"] is False


def test_briefing_items_and_status(paths, box):
    from navig_email.store import get_store

    get_store()._write("INSERT INTO digest_items (item_id, fingerprint, kind, title, data_json, first_seen) "
                       "VALUES ('fp1','fp1','job','Lead QA','{}','2026-10-19')")
    status, data = call("GET", "/api/deck/email/mailroom/briefing")
    assert data["data"]["items"][0]["title"] == "Lead QA"
    assert call("POST", "/api/deck/email/mailroom/briefing/item", json={"item": "fp1", "status": "saved"})[0] == 200
    assert call("POST", "/api/deck/email/mailroom/briefing/item", json={"item": "fp1", "status": "deleted"})[0] == 400


def test_accounts_disable_and_history(paths, box, monkeypatch):
    from navig_email import account, gmail
    from navig_email import overview as O

    monkeypatch.setattr(account, "linked_accounts", lambda: ["box@gmail.com"])
    monkeypatch.setattr(gmail, "is_connected", lambda a=None: True)
    monkeypatch.setattr(O, "resolve_space", lambda s=None: paths)
    status, data = call("POST", "/api/deck/email/mailroom/accounts", json={"account": "box@gmail.com", "disabled": True})
    row = data["data"]["accounts"][0]
    assert row["disabled"] is True and row["aliases"] == ["hello@studio.test"]
    status, data = call("GET", "/api/deck/email/mailroom/history")
    assert any(r["action"] == "account.disable" for r in data["data"]["audit"])
