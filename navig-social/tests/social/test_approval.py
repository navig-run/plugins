"""G1 — content approval: bind, invalidate, gate, replay, restart (SOCIAL-CONTRACT §4)."""
from __future__ import annotations

import asyncio

import pytest

from navig.store.scheduled_posts import ScheduledPostStore
from navig_social.social.scheduler_service import ScheduledPostService
from navig_social.social.types import PublishReceipt


@pytest.fixture()
def store(tmp_path):
    return ScheduledPostStore(db_path=tmp_path / "sp.db")


def _mk(store, *, policy="required", body="hello"):
    return store.create(body=body,
                        content={"body": body, "approval_policy": policy},
                        targets=[{"network": "telegram", "target": "@sch8ma"}],
                        status="scheduled", schedule_kind="once",
                        run_at="2020-01-01T00:00:00Z")


class _FakeDispatcher:
    def __init__(self):
        self.calls = 0

    async def publish(self, content, targets):
        self.calls += 1
        return [PublishReceipt.success("telegram", "@sch8ma", id="m1", url="https://t.me/x/1")]


def _svc(fake):
    svc = ScheduledPostService(None)
    svc._dispatcher = lambda: fake  # instance-level, no monkeypatch plumbing
    return svc


def test_required_policy_blocks_publish_until_approved(store):
    fake = _FakeDispatcher()
    pid = _mk(store)
    post = store.get(pid)
    assert store.approval_ok(post) is False

    res = asyncio.run(_svc(fake).run_post(store, post))
    assert res["status"] == "awaiting_approval"
    assert fake.calls == 0, "must not publish before approval"
    assert store.get(pid)["status"] == "scheduled"  # never left the queue
    assert store.get(pid)["last_error"] == "awaiting approval"

    store.approve(pid, decided_by="owner")
    post = store.get(pid)
    assert store.approval_ok(post) is True
    res = asyncio.run(_svc(fake).run_post(store, post))
    assert res["status"] == "published"
    assert fake.calls == 1


def test_auto_policy_publishes_without_approval(store):
    fake = _FakeDispatcher()
    pid = _mk(store, policy="auto")
    res = asyncio.run(_svc(fake).run_post(store, store.get(pid)))
    assert res["status"] == "published" and fake.calls == 1
    # and YouTube-style behaviour is untouched even with an explicit auto
    store.update(pid, status="scheduled")
    res2 = asyncio.run(_svc(fake).run_post(store, store.get(pid)))
    assert res2["status"] == "published" and fake.calls == 2


def test_edit_invalidates_an_approval_and_reapprove_works(store):
    pid = _mk(store)
    store.approve(pid, decided_by="owner")
    assert store.approval_ok(store.get(pid)) is True

    edited = store.set_content(pid, {"body": "EDITED", "approval_policy": "required"},
                               [{"network": "telegram", "target": "@sch8ma"}])
    assert edited["version"] == 2
    assert edited["approval_state"] == "invalidated"
    assert store.approval_ok(edited) is False
    # history row exists
    n = store._read_one(
        "SELECT COUNT(*) AS c FROM post_versions WHERE post_id = ?", (pid,))["c"]
    assert n == 1

    store.approve(pid, decided_by="owner")
    assert store.approval_ok(store.get(pid)) is True


def test_stale_hash_approval_is_refused(store):
    pid = _mk(store)
    with pytest.raises(ValueError, match="stale approval"):
        store.approve(pid, content_hash="sha256:" + "0" * 64)
    # matching hash is fine
    store.approve(pid, content_hash=store.get(pid)["content_hash"])
    assert store.get(pid)["approval_state"] == "approved"


def test_reject_marks_rejected(store):
    pid = _mk(store)
    store.reject(pid, reason="not on brand", decided_by="owner")
    post = store.get(pid)
    assert post["approval_state"] == "rejected"
    assert store.approval_ok(post) is False


def test_reschedule_does_not_void_or_bump(store):
    pid = _mk(store)
    store.approve(pid, decided_by="owner")
    store.update(pid, run_at="2030-01-01T00:00:00Z")   # schedule change = legal
    post = store.get(pid)
    assert post["approval_state"] == "approved"
    assert post["version"] == 1
    assert store.approval_ok(post) is True


def test_replayed_key_never_publishes_twice(store, tmp_path):
    fake = _FakeDispatcher()
    pid = _mk(store, policy="auto")
    post = store.get(pid)
    res = asyncio.run(_svc(fake).run_post(store, post))
    assert res["status"] == "published" and fake.calls == 1

    key = f"{post['content_hash']}:{post['run_at']}"
    stored = store.get(pid)
    assert stored["idempotency_key"] == key
    again = asyncio.run(_svc(fake).run_post(store, stored, idempotency_key=key))
    assert again["status"] == "replayed"
    assert fake.calls == 1, "replay must not publish again"


def test_approval_state_survives_restart(tmp_path):
    db = tmp_path / "sp.db"
    s1 = ScheduledPostStore(db_path=db)
    pid = _mk(s1)
    s1.approve(pid, decided_by="owner")
    # a brand-new store instance on the same file (process restart)
    s2 = ScheduledPostStore(db_path=db)
    post = s2.get(pid)
    assert post["approval_state"] == "approved"
    assert s2.approval_ok(post) is True
    assert post["content_hash"]
