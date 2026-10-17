"""G2 — versioning: hash at create, bump on material edit, immutability of history."""
from __future__ import annotations

import pytest

from navig.store.scheduled_posts import ScheduledPostStore
from navig_social.social.content_hash import of_post


@pytest.fixture()
def store(tmp_path):
    return ScheduledPostStore(db_path=tmp_path / "sp.db")


def test_create_computes_identity_and_version_1(store):
    content = {"body": "hello", "approval_policy": "required",
               "content_id": "schema:wave1:2026-10-15-tx-002"}
    targets = [{"network": "telegram", "target": "@sch8ma"}]
    pid = store.create(content=content, targets=targets, status="draft")
    post = store.get(pid)
    assert post["content_hash"] == of_post(content, targets)
    assert post["version"] == 1
    assert post["content_id"] == "schema:wave1:2026-10-15-tx-002"
    assert post["approval_state"] == "none"


def test_explicit_contract_hash_is_kept_verbatim(store):
    pinned = "sha256:" + "c" * 64
    pid = store.create(content={"body": "x", "content_hash": pinned}, targets=[])
    assert store.get(pid)["content_hash"] == pinned


def test_same_content_does_not_bump_version(store):
    pid = store.create(content={"body": "same"}, targets=[{"network": "t"}])
    before = store.get(pid)["content_hash"]
    post = store.set_content(pid, {"body": "same"}, [{"network": "t"}])
    assert post["version"] == 1
    assert post["content_hash"] == before
    n = store._read_one(
        "SELECT COUNT(*) AS c FROM post_versions WHERE post_id = ?", (pid,))["c"]
    assert n == 0, "no history row without a content change"


def test_history_rows_are_append_only_records(store):
    pid = store.create(content={"body": "v1"}, targets=[])
    store.set_content(pid, {"body": "v2"}, [])
    store.set_content(pid, {"body": "v3"}, [])
    rows = store._read_all(
        "SELECT version, content_json FROM post_versions WHERE post_id = ? "
        "ORDER BY version", (pid,))
    assert [r["version"] for r in rows] == [2, 3]
    assert '"v2"' in rows[0]["content_json"] and '"v3"' in rows[1]["content_json"]
