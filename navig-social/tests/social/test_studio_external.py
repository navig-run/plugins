"""G6 — idempotent contract submission + route gates (SOCIAL-CONTRACT §4/§6).

The producer-facing seam: one canonical content item addressed by
``content.content_id``; a retried submit replays instead of duplicating; a
changed hash versions and re-reviews; publishes replay under an idempotency
key (header or body), refuse without a bound approval (409), refuse after
expiry (410), and refuse content edits on terminal posts (409 immutable).
Everything runs against a per-test store and a fake dispatcher — no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import navig.store.scheduled_posts as sp
from navig.store.scheduled_posts import ScheduledPostStore
from navig_social.social.types import PublishReceipt

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "schema-wave1-2026-10-15-tx-002.contract.json"
CID = "schema:wave1:2026-10-15-tx-002"


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = ScheduledPostStore(db_path=tmp_path / "posts.db")
    monkeypatch.setattr(sp, "_store", s)
    return s


class FakeRequest:
    def __init__(self, match=None, query=None, body=None, headers=None):
        self.match_info = match or {}
        self.query = query or {}
        self.headers = headers or {}
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _payload(resp):
    return json.loads(resp.body)


def _fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _submit_body(**over):
    contract = _fixture()
    body = {
        "external": {
            "content_id": contract["content_id"],
            "project_id": contract["project_id"],
            "campaign_id": contract["campaign_id"],
            "content_hash": contract["content_hash"],
            "version": contract["version"],
            "approval_policy": "auto",
        },
        "body": contract["captions"]["canonical"],
        "targets": [{"network": "telegram", "target": "-1001269675237"}],
        "content": {"contract": contract},
        "status": "draft",
        "schedule_kind": "once",
        "run_at": "2026-10-15T08:00:00Z",
    }
    body.update(over)
    return body


class FakeDispatcher:
    def __init__(self, ok: bool = True):
        self.calls: list = []
        self.ok = ok

    async def publish(self, content, targets):
        self.calls.append({"content": content, "targets": list(targets)})
        out = []
        for t in targets:
            net, tgt = t.get("network", ""), t.get("target", "")
            if self.ok:
                out.append(PublishReceipt.success(net, tgt, id="m1", url="https://t.me/sch8ma/1"))
            else:
                out.append(PublishReceipt.failure(net, tgt, "boom"))
        return out


@pytest.fixture
def fake_dispatch(monkeypatch):
    from navig_social.social.scheduler_service import ScheduledPostService

    disp = FakeDispatcher()
    monkeypatch.setattr(ScheduledPostService, "_dispatcher", lambda self: disp)
    return disp


async def _submit(body=None):
    from navig_social.deck_routes import studio as st

    return await st.handle_studio_post_create(FakeRequest(body=body or _submit_body()))


# ── submission / upsert ─────────────────────────────────────────────────────

async def test_submit_creates_external_row(store):
    resp = await _submit()
    assert resp.status == 201
    data = _payload(resp)["data"]
    assert data["acceptance"] == "created"
    post = data["post"]
    assert post["content_id"] == CID
    assert post["content_hash"] == _fixture()["content_hash"]   # §3 passthrough
    assert post["campaign"] == "wave1-launch"
    assert data["approval"]["state"] == "none"                 # approval_policy=auto
    assert data["validation"]["ok"] is True
    assert post["body"] == _fixture()["captions"]["canonical"]  # projection
    assert "telegram" in post["content"]["per_network"]
    assert {m["network"] for m in data["manual_handoff"]} == {"instagram", "vk"}


async def test_resubmit_same_hash_replays_without_duplicating(store):
    first = await _submit()
    second = await _submit()
    assert first.status == 201 and second.status == 200
    assert _payload(second)["data"]["acceptance"] == "replay"
    assert _payload(second)["data"]["post"]["id"] == _payload(first)["data"]["post"]["id"]
    assert len(store.list()) == 1


async def test_resubmit_changed_hash_versions_and_re_reviews(store):
    body = _submit_body(external={**_submit_body()["external"], "approval_policy": "required"})
    created = _payload(await _submit(body))["data"]
    assert created["approval"]["state"] == "pending"      # required → pending on submit
    pid = created["post"]["id"]

    from navig_social.deck_routes import studio as st

    ok = await st.handle_studio_post_approve(
        FakeRequest(match={"id": str(pid)}, body={"by": "owner"}))
    assert _payload(ok)["data"]["post"]["approval_state"] == "approved"

    changed = _fixture()
    changed["captions"]["canonical"] = "TRANSMISSION 002 — edited"
    from navig_social.social.content_hash import contract_hash

    new_hash = contract_hash(changed)
    body2 = _submit_body(
        body=changed["captions"]["canonical"], content={"contract": changed},
        external={**body["external"], "content_hash": new_hash, "version": 2},
    )
    resp = await _submit(body2)
    assert resp.status == 200
    data = _payload(resp)["data"]
    assert data["acceptance"] == "updated"
    assert data["post"]["version"] == 2
    assert data["approval"]["state"] == "pending"         # re-review (§4 step 7)
    states = [r["state"] for r in store._read_all(
        "SELECT state FROM post_approvals WHERE post_id = ? ORDER BY id", (pid,))]
    assert states[:2] == ["pending", "approved"] and "invalidated" in states
    # history semantics (pinned by test_versioning): append-only CHANGE records —
    # the edit lands as version 2; v1 is the row's creation state, not duplicated
    hist = [r["version"] for r in store._read_all(
        "SELECT version FROM post_versions WHERE post_id = ? ORDER BY version", (pid,))]
    assert hist == [2]


async def test_resubmit_after_publish_conflicts(store, fake_dispatch):
    from navig_social.deck_routes import studio as st

    created = _payload(await _submit())["data"]
    pid = created["post"]["id"]
    await st.handle_studio_post_update(FakeRequest(match={"id": str(pid)},
                                                   body={"status": "scheduled"}))
    pub = await st.handle_studio_post_publish(FakeRequest(match={"id": str(pid)}))
    assert pub.status == 200 and _payload(pub)["data"]["status"] == "published"

    changed = _fixture()
    changed["captions"]["canonical"] = "too late to edit"
    from navig_social.social.content_hash import contract_hash

    resp = await _submit(_submit_body(
        body="too late to edit", content={"contract": changed},
        external={**_submit_body()["external"], "content_hash": contract_hash(changed)}))
    assert resp.status == 409
    assert _payload(resp)["code"] == "idempotency_conflict"


async def test_submit_validation_rejects_phase1_tokens_and_stray_targets(store):
    bad = _fixture()
    bad["captions"]["canonical"] = "sign up now at https://evil.example"
    resp = await _submit(_submit_body(
        body=bad["captions"]["canonical"], content={"contract": bad},
        external={**_submit_body()["external"], "content_hash": "sha256:whatever"}))
    assert resp.status == 422
    err = _payload(resp)
    assert err["code"] == "validation_failed"
    assert any("phase-1" in e for e in err["details"]["validation"]["errors"])
    assert store.list() == []

    resp2 = await _submit(_submit_body(targets=[{"network": "reddit", "target": "r/x"}]))
    assert resp2.status == 422
    assert any("not among contract destinations" in e
               for e in _payload(resp2)["details"]["validation"]["errors"])


async def test_missing_content_id_is_422(store):
    resp = await _submit(_submit_body(external={"project_id": "schema-wave1"}))
    assert resp.status == 422
    assert "content_id" in str(_payload(resp)["details"])


# ── PATCH: immutability + contract authority ────────────────────────────────

async def test_patch_on_published_post_is_409_immutable(store, fake_dispatch):
    from navig_social.deck_routes import studio as st

    pid = _payload(await _submit())["data"]["post"]["id"]
    await st.handle_studio_post_update(FakeRequest(match={"id": str(pid)},
                                                   body={"status": "scheduled"}))
    assert (await st.handle_studio_post_publish(
        FakeRequest(match={"id": str(pid)}))).status == 200

    resp = await st.handle_studio_post_update(FakeRequest(match={"id": str(pid)},
                                                          body={"body": "hacked"}))
    assert resp.status == 409
    assert _payload(resp)["code"] == "immutable"
    assert store.get(pid)["body"] != "hacked"


async def test_patch_external_content_requires_identity_in_payload(store):
    from navig_social.deck_routes import studio as st

    pid = _payload(await _submit())["data"]["post"]["id"]
    resp = await st.handle_studio_post_update(
        FakeRequest(match={"id": str(pid)}, body={"content": {"body": "stripped"}}))
    assert resp.status == 422
    assert _payload(resp)["code"] == "contract_required"


async def test_patch_with_contract_bumps_version_and_rehashes(store):
    from navig_social.deck_routes import studio as st
    from navig_social.social.content_hash import contract_hash

    pid = _payload(await _submit())["data"]["post"]["id"]
    edited = _fixture()
    edited["captions"]["canonical"] = "caption replaced"
    resp = await st.handle_studio_post_update(FakeRequest(
        match={"id": str(pid)},
        body={"content": {"contract": edited, "content_hash": contract_hash(edited)},
              "body": "caption replaced"}))
    assert resp.status == 200
    post = _payload(resp)["data"]["post"]
    assert post["version"] == 2
    assert post["content_hash"] == contract_hash(edited)
    assert post["body"] == "caption replaced"
    hist = store._read_all("SELECT version FROM post_versions WHERE post_id = ? ORDER BY version",
                           (pid,))
    assert [h["version"] for h in hist] == [2]   # change-record semantics (see test_versioning)


async def test_reschedule_patch_keeps_approval(store):
    from navig_social.deck_routes import studio as st

    body = _submit_body(external={**_submit_body()["external"], "approval_policy": "required"})
    pid = _payload(await _submit(body))["data"]["post"]["id"]
    await st.handle_studio_post_approve(FakeRequest(match={"id": str(pid)}))
    resp = await st.handle_studio_post_update(
        FakeRequest(match={"id": str(pid)}, body={"run_at": "2026-10-16T08:00:00Z"}))
    assert resp.status == 200
    assert _payload(resp)["data"]["post"]["approval_state"] == "approved"  # §3


# ── publish: idempotency, approval gate, expiry ─────────────────────────────

async def test_double_publish_same_key_sends_once(store, fake_dispatch):
    from navig_social.deck_routes import studio as st

    pid = _payload(await _submit())["data"]["post"]["id"]
    headers = {"Idempotency-Key": "schema:wave1:tx-002:hash:v1"}
    first = await st.handle_studio_post_publish(
        FakeRequest(match={"id": str(pid)}, headers=headers))
    second = await st.handle_studio_post_publish(
        FakeRequest(match={"id": str(pid)}, headers=headers))
    assert first.status == 200 and second.status == 200
    assert _payload(second)["data"]["replayed"] is True
    assert len(fake_dispatch.calls) == 1                    # exactly one send


async def test_publish_without_required_approval_is_409(store, fake_dispatch):
    from navig_social.deck_routes import studio as st

    body = _submit_body(external={**_submit_body()["external"], "approval_policy": "required"})
    pid = _payload(await _submit(body))["data"]["post"]["id"]
    resp = await st.handle_studio_post_publish(FakeRequest(match={"id": str(pid)}))
    assert resp.status == 409
    assert _payload(resp)["code"] == "approval_required"
    assert fake_dispatch.calls == []
    assert store.get(pid)["status"] == "draft"             # never entered publishing

    await st.handle_studio_post_approve(FakeRequest(match={"id": str(pid)}))
    ok = await st.handle_studio_post_publish(FakeRequest(match={"id": str(pid)}))
    assert ok.status == 200 and fake_dispatch.calls


async def test_stale_approve_is_refused(store):
    from navig_social.deck_routes import studio as st

    pid = _payload(await _submit())["data"]["post"]["id"]
    resp = await st.handle_studio_post_approve(
        FakeRequest(match={"id": str(pid)}, body={"content_hash": "sha256:stale"}))
    assert resp.status == 409
    assert store.get(pid)["approval_state"] == "none"


async def test_expired_post_publishes_nothing(store, fake_dispatch):
    from navig_social.deck_routes import studio as st

    pid = _payload(await _submit())["data"]["post"]["id"]
    await st.handle_studio_post_update(
        FakeRequest(match={"id": str(pid)},
                    body={"expires_at": "2026-01-01T00:00:00Z", "status": "scheduled"}))
    resp = await st.handle_studio_post_publish(FakeRequest(match={"id": str(pid)}))
    assert resp.status == 410
    assert _payload(resp)["code"] == "expired"
    assert fake_dispatch.calls == []


async def test_publish_failure_is_retryable_under_the_same_key(store, fake_dispatch):
    """G5 protects successes, not attempts: a FAILED publish may be retried with
    the same key (explicit re-POST = 'try again'), while a success replays
    (pinned by test_double_publish_same_key_sends_once)."""
    from navig_social.deck_routes import studio as st

    fake_dispatch.ok = False
    pid = _payload(await _submit())["data"]["post"]["id"]
    headers = {"Idempotency-Key": "k-retry"}
    resp = await st.handle_studio_post_publish(FakeRequest(match={"id": str(pid)},
                                                           headers=headers))
    data = _payload(resp)["data"]
    assert data["status"] == "failed"
    assert data["receipts"][0]["ok"] is False

    fake_dispatch.ok = True                       # the fix landed; retry succeeds
    retry = await st.handle_studio_post_publish(FakeRequest(match={"id": str(pid)},
                                                            headers=headers))
    assert retry.status == 200
    assert _payload(retry)["data"]["status"] == "published"
    assert len(fake_dispatch.calls) == 2          # one fail + one retry, no more


# ── queries ─────────────────────────────────────────────────────────────────

async def test_list_filters_by_campaign_and_content_id(store):
    await _submit()
    from navig_social.deck_routes import studio as st

    by_campaign = await st.handle_studio_posts_list(FakeRequest(query={"campaign": "wave1-launch"}))
    assert len(_payload(by_campaign)["data"]["posts"]) == 1
    by_id = await st.handle_studio_posts_list(FakeRequest(query={"content_id": CID}))
    assert len(_payload(by_id)["data"]["posts"]) == 1
    none = await st.handle_studio_posts_list(FakeRequest(query={"campaign": "nope"}))
    assert _payload(none)["data"]["posts"] == []


async def test_deck_native_create_shape_unchanged(store):
    """Back-compat: a plain create still answers {post} with 201 (deck UI)."""
    from navig_social.deck_routes import studio as st

    resp = await st.handle_studio_post_create(FakeRequest(body={
        "body": "native", "targets": [{"network": "twitter", "target": ""}],
        "status": "draft",
    }))
    assert resp.status == 201
    data = _payload(resp)["data"]
    assert data["post"]["body"] == "native"
    assert data["post"]["content_hash"].startswith("sha256:")   # hashed from birth
    assert "acceptance" not in data                              # envelope is external-only
