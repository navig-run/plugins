"""Store v3 — campaign grouping, publish expiry, terminal immutability.

The three seams the Stage-B (G1–G5) store did not carry: a ``campaign`` column
for grouping, ``expires_at`` with an expiry that NEVER publishes, and the rule
that content cannot change once a post is (being) published. Migration tests
assert ``== ScheduledPostStore.SCHEMA_VERSION`` — never a literal.
"""

from __future__ import annotations

import sqlite3

import pytest

from navig.store.scheduled_posts import ScheduledPostStore


@pytest.fixture
def store(tmp_path):
    return ScheduledPostStore(tmp_path / "posts.db")


# ── migration ───────────────────────────────────────────────────────────────

def test_fresh_db_stamped_at_schema_version(store):
    assert store.get_schema_version() == ScheduledPostStore.SCHEMA_VERSION


def test_v2_database_migrates_to_v3_preserving_rows(tmp_path):
    """A genuine Stage-B v2 db opens, gains campaign/expires_at, keeps data."""
    db = tmp_path / "v2.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
        CREATE TABLE scheduled_posts (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            body           TEXT NOT NULL DEFAULT '',
            content_json   TEXT NOT NULL DEFAULT '{}',
            targets_json   TEXT NOT NULL DEFAULT '[]',
            status         TEXT NOT NULL DEFAULT 'draft',
            schedule_kind  TEXT NOT NULL DEFAULT 'now',
            run_at         TEXT,
            cron_expr      TEXT,
            receipts_json  TEXT NOT NULL DEFAULT '[]',
            last_error     TEXT,
            content_id     TEXT,
            content_hash   TEXT,
            version        INTEGER NOT NULL DEFAULT 1,
            approval_state TEXT NOT NULL DEFAULT 'none',
            approved_hash  TEXT,
            approved_at    TEXT,
            idempotency_key TEXT,
            created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
        INSERT INTO schema_version (version) VALUES (2);
        INSERT INTO scheduled_posts (body, status, content_id, content_hash)
        VALUES ('old', 'scheduled', 'schema:wave1:x', 'sha256:old');
    """)
    conn.commit()
    conn.close()

    s = ScheduledPostStore(db)
    assert s.get_schema_version() == ScheduledPostStore.SCHEMA_VERSION
    rows = s.list()
    assert len(rows) == 1
    assert rows[0]["body"] == "old" and rows[0]["content_id"] == "schema:wave1:x"
    assert rows[0]["campaign"] is None and rows[0]["expires_at"] is None


# ── expiry ──────────────────────────────────────────────────────────────────

def test_expire_due_parks_and_due_excludes(store):
    past = store.create(body="stale", status="scheduled", run_at="2026-01-01T00:00:00Z",
                        expires_at="2026-01-01T23:59:00Z", campaign="wave1-launch",
                        content={"content_id": "schema:wave1:e1"})
    store.pend_for_review(past, decided_by="submit")
    live = store.create(body="live", status="scheduled", run_at="2026-01-01T00:00:00Z",
                        expires_at="2099-01-01T00:00:00Z")

    now = "2026-01-02T00:00:00Z"
    # belt: even before expire_due runs, due() refuses the expired row
    assert [p["id"] for p in store.due(now)] == [live]

    assert store.expire_due(now) == 1
    parked = store.get(past)
    assert parked["status"] == "expired"
    assert parked["approval_state"] == "expired"      # pending → expired
    assert store.get(live)["status"] == "scheduled"   # untouched
    assert store.due("2099-01-01T00:00:00Z") == []    # live's own deadline also excludes


def test_expiry_never_rewrites_an_approval_decision(store):
    ok = store.create(body="approved", status="scheduled", run_at="2099-01-01T00:00:00Z",
                      expires_at="2026-01-01T00:00:00Z")
    store.approve(ok, decided_by="owner")
    assert store.expire_due("2026-06-01T00:00:00Z") == 1
    row = store.get(ok)
    assert row["status"] == "expired"
    assert row["approval_state"] == "approved"        # the decision stands


def test_expires_at_is_canonicalised_on_create(tmp_path):
    s = ScheduledPostStore(tmp_path / "p.db")
    pid = s.create(body="x", status="scheduled", run_at="2026-11-01T10:00:00+02:00",
                   expires_at="2026-11-01T23:59:00+01:00")
    assert s.get(pid)["expires_at"] == "2026-11-01T22:59:00Z"   # lexicographic-safe UTC


# ── immutability ────────────────────────────────────────────────────────────

def test_update_refuses_content_on_terminal_status(store):
    pid = store.create(body="live", status="published")
    assert store.update(pid, body="edited") is False
    assert store.update(pid, content={"x": 1}) is False
    assert store.update(pid, targets=[]) is False
    assert store.get(pid)["body"] == "live"
    # schedule/annotation fields stay editable
    assert store.update(pid, last_error="annotated") is True
    assert store.update(pid, run_at="2026-11-01T10:00:00Z") is True


def test_set_content_refuses_terminal_with_named_error(store):
    pid = store.create(body="done", status="published")
    with pytest.raises(ValueError, match="immutable"):
        store.set_content(pid, {"body": "nope"})


def test_set_content_still_edits_drafts_and_bumps_version(store):
    pid = store.create(body="v1", status="draft", content={"body": "v1"})
    out = store.set_content(pid, {"body": "v2"})
    assert out["version"] == 2
    assert out["approval_state"] == "none"            # nothing approved yet


def test_update_still_edits_draft_content(store):
    pid = store.create(body="draft", status="draft")
    assert store.update(pid, body="better") is True
    assert store.get(pid)["body"] == "better"


# ── campaign / filters / review-pending ─────────────────────────────────────

def test_campaign_and_content_id_filters(store):
    store.create(body="a", campaign="wave1-launch", content={"content_id": "schema:wave1:a"})
    store.create(body="b", campaign="wave1-launch", content={"content_id": "schema:wave1:b"})
    store.create(body="c", campaign="other")
    assert len(store.list(campaign="wave1-launch")) == 2
    assert [p["body"] for p in store.list(content_id="schema:wave1:a")] == ["a"]
    assert store.list(campaign="wave1-launch", content_id="schema:wave1:b")[0]["body"] == "b"
    assert store.list(campaign="nope") == []


def test_pend_for_review_is_idempotent_and_audited(store):
    pid = store.create(body="x", status="scheduled")
    assert store.pend_for_review(pid) is True
    assert store.pend_for_review(pid) is True          # already pending — no double row
    assert store.get(pid)["approval_state"] == "pending"
    rows = store._read_all(
        "SELECT state FROM post_approvals WHERE post_id = ? ORDER BY id", (pid,))
    assert [r["state"] for r in rows] == ["pending"]   # exactly one audit row
    assert store.pend_for_review(4242) is False


# ── v1 database opens (the silent-brick regression, live 2026-10-15) ───────

def test_v1_database_migrates_all_the_way(tmp_path):
    """A real pre-#1959 v1 scheduled_posts.db must OPEN and migrate.

    The bug this pins: ``_create_schema`` created ``idx_sched_content`` (on the
    v2 ``content_id`` column) inside its script, and BaseStore runs that BEFORE
    the version check — on a v1 table it raised ``no such column: content_id``,
    store init died, and every Studio route + the scheduler tick 500'd on a live
    install whose db predated v2 (same class as the receipts-ledger brick).
    """
    db = tmp_path / "v1.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
        CREATE TABLE scheduled_posts (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            body           TEXT NOT NULL DEFAULT '',
            content_json   TEXT NOT NULL DEFAULT '{}',
            targets_json   TEXT NOT NULL DEFAULT '[]',
            status         TEXT NOT NULL DEFAULT 'draft',
            schedule_kind  TEXT NOT NULL DEFAULT 'now',
            run_at         TEXT,
            cron_expr      TEXT,
            receipts_json  TEXT NOT NULL DEFAULT '[]',
            last_error     TEXT,
            created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
        CREATE INDEX idx_sched_due ON scheduled_posts (status, run_at);
        INSERT INTO schema_version (version) VALUES (1);
        INSERT INTO scheduled_posts (body, status) VALUES ('legacy', 'draft');
    """)
    conn.commit()
    conn.close()

    s = ScheduledPostStore(db)
    assert s.get_schema_version() == ScheduledPostStore.SCHEMA_VERSION
    rows = s.list()
    assert len(rows) == 1 and rows[0]["body"] == "legacy"
    assert rows[0]["approval_state"] == "none" and rows[0]["campaign"] is None
    # v2/v3 writes work on the migrated table; the content index now exists
    pid = s.create(body="new", content={"content_id": "schema:wave1:x"}, campaign="c")
    assert s.list(content_id="schema:wave1:x")[0]["id"] == pid
    idx = {r[1] for r in s._get_conn().execute("PRAGMA index_list(scheduled_posts)")}
    assert "idx_sched_content" in idx
    s.close()
    # a second open is idempotent (no duplicate-column death on re-run)
    again = ScheduledPostStore(db)
    assert len(again.list()) == 2
