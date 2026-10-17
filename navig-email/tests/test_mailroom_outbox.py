"""The outbox state machine: approval binds text, cancel beats dispatch, no blind resend.

Scenario letters refer to the Mailroom 2.0 spec: D (draft only), E (scheduled + cancel),
I (provider accepted, response lost), J (two dispatchers), K (restart mid-send),
L (multi-account isolation).
"""

from __future__ import annotations

import asyncio
import threading
from datetime import timedelta

import pytest

from fake_gmail import FakeGmail, raw_message
from navig_email import outbox as ob
from navig_email.space import MailroomPaths

ACCOUNTS = """
version: 1
accounts:
  - id: studio
    address: box@gmail.com
    aliases:
      - { address: support@studio.test, notify: true }
      - { address: hello@studio.test }
  - id: dev
    address: dev@gmail.com
"""


@pytest.fixture
def paths(tmp_path) -> MailroomPaths:
    root = tmp_path / "space"
    (root / ".navig").mkdir(parents=True)
    (root / "mailroom").mkdir(parents=True)
    (root / ".navig" / "config.yaml").write_text("mailroom: {}\n", encoding="utf-8")
    (root / "mailroom" / "accounts.yaml").write_text(ACCOUNTS, encoding="utf-8")
    return MailroomPaths(root)


@pytest.fixture
def gmail(monkeypatch) -> FakeGmail:
    fake = FakeGmail("box@gmail.com")

    async def factory(_account):
        return fake

    monkeypatch.setattr(ob, "CONNECTOR_FACTORY", factory)
    monkeypatch.setattr(ob, "ACCOUNT_CHECK", lambda _a: True)
    return fake


def _job(paths, **kw):
    base = dict(account="box@gmail.com", to="Ann <ann@client.test>", subject="Re: hi", body="Hello Ann, thanks.",
                from_addr="support@studio.test", paths=paths, thread_id="t1", in_reply_to="<m1@client>")
    base.update(kw)
    return ob.create(**base)


def _approve(job, *, delay=timedelta(0)):
    return ob.approve(job["job_id"], content_hash_seen=job["content_hash"], send_at=ob.now_utc() + delay)


def run(coro):
    return asyncio.run(coro)


# ── creation & validation ───────────────────────────────────────────────────


def test_a_new_job_is_a_draft_and_nothing_is_sent(paths, gmail, mailroom_events):  # scenario D
    job = _job(paths)
    assert job["state"] == ob.DRAFT
    stats = run(ob.tick())
    assert stats["sent"] == 0 and gmail.sent_messages == []
    assert ("draft", job["job_id"], "") in mailroom_events
    assert not any(ev == "sent" for ev, *_ in mailroom_events)


def test_header_injection_and_bad_addresses_are_refused(paths):
    with pytest.raises(ob.OutboxError):
        _job(paths, to="ann@client.test\nBcc: everyone@x.test")
    with pytest.raises(ob.OutboxError):
        _job(paths, subject="hi\r\nBcc: x@y.z")
    with pytest.raises(ob.OutboxError):
        _job(paths, to="not an address")
    with pytest.raises(ob.OutboxError):
        _job(paths, to=", ".join(f"u{i}@x.test" for i in range(ob.MAX_RECIPIENTS + 1)))


def test_an_alias_of_another_account_is_refused(paths):  # scenario L
    with pytest.raises(ob.OutboxError, match="not an address of"):
        _job(paths, account="dev@gmail.com", from_addr="support@studio.test")
    with pytest.raises(ob.OutboxError, match="needs the space"):
        _job(None, from_addr="support@studio.test")
    assert _job(paths, account="dev@gmail.com", from_addr="dev@gmail.com")["from_addr"] == "dev@gmail.com"


def test_a_policy_reply_to_one_message_is_queued_once(paths):
    a = _job(paths, origin="policy", policy_id="ack", reply_to_msg="m1")
    b = _job(paths, origin="policy", policy_id="ack", reply_to_msg="m1", body="Different text")
    assert a["job_id"] == b["job_id"]
    # a person asking twice means twice
    assert _job(paths)["job_id"] != _job(paths)["job_id"]


# ── approval binds the text ────────────────────────────────────────────────


def test_approval_requires_the_hash_the_approver_saw(paths):
    job = ob.submit(_job(paths)["job_id"])
    with pytest.raises(ob.Conflict):
        ob.approve(job["job_id"], content_hash_seen="0" * 64)
    done = ob.approve(job["job_id"], content_hash_seen=job["content_hash"])
    assert done["state"] == ob.SCHEDULED and done["approved_hash"] == job["content_hash"]
    assert ob.parse_iso(done["not_before"]) - ob.now_utc() > timedelta(minutes=14)  # 15 min default


def test_an_edit_after_approval_drops_the_approval(paths, gmail):
    job = _approve(_job(paths))
    edited = ob.edit(job["job_id"], body="Hello Ann, a better text.")
    assert edited["state"] == ob.PENDING_APPROVAL
    assert edited["approved_hash"] == "" and edited["not_before"] is None
    assert run(ob.tick())["sent"] == 0 and gmail.sent_messages == []


def test_a_row_changed_behind_the_approval_is_not_sent(paths, gmail):
    job = _approve(_job(paths))
    store = ob._store(None)
    store._write("UPDATE outbox SET body = 'tampered' WHERE job_id = ?", (job["job_id"],))
    run(ob.tick())
    assert gmail.sent_messages == []
    assert ob.require(job["job_id"])["state"] == ob.NEEDS_REVIEW


# ── scheduled send & cancel (scenario E) ───────────────────────────────────


def test_scheduled_send_waits_then_sends_once_with_its_own_message_id(paths, gmail, mailroom_events):
    job = _approve(_job(paths), delay=timedelta(minutes=15))
    assert run(ob.tick())["sent"] == 0
    later = ob.now_utc() + timedelta(minutes=16)
    assert run(ob.tick(now=later))["sent"] == 1
    assert run(ob.tick(now=later + timedelta(minutes=1)))["sent"] == 0
    assert len(gmail.sent_messages) == 1
    msg = gmail.sent_messages[0]["msg"]
    assert msg["Message-ID"] == job["message_id"] and msg["X-Navig-Job"] == job["job_id"]
    assert msg["In-Reply-To"] == "<m1@client>" and gmail.sent_messages[0]["thread_id"] == "t1"
    assert "support@studio.test" in msg["From"]
    final = ob.require(job["job_id"])
    assert final["state"] == ob.SENT and final["provider_msg_id"] == "s1"
    assert [ev for ev, *_ in mailroom_events].count("sent") == 1


def test_cancel_prevents_dispatch(paths, gmail):
    job = _approve(_job(paths), delay=timedelta(minutes=15))
    ob.cancel(job["job_id"])
    run(ob.tick(now=ob.now_utc() + timedelta(hours=1)))
    assert gmail.sent_messages == []
    with pytest.raises(ob.OutboxError):
        ob.approve(job["job_id"], content_hash_seen=job["content_hash"])


def test_cancel_wins_against_a_claim_that_has_not_started_sending(paths, gmail):
    job = _approve(_job(paths))
    [claimed] = ob.claim_due()
    ob.cancel(job["job_id"])  # the person is faster than the dispatcher
    with pytest.raises(ob.Conflict):
        run(ob.dispatch(claimed))
    assert gmail.sent_messages == [] and ob.require(job["job_id"])["state"] == ob.CANCELLED


def test_delay_and_send_now(paths, gmail):
    job = _approve(_job(paths), delay=timedelta(minutes=15))
    moved = ob.reschedule(job["job_id"], ob.now_utc() + timedelta(hours=2))
    assert run(ob.tick(now=ob.now_utc() + timedelta(minutes=30)))["sent"] == 0
    ob.reschedule(moved["job_id"], ob.now_utc())
    assert run(ob.tick())["sent"] == 1


# ── pause ──────────────────────────────────────────────────────────────────


def test_pause_auto_holds_policy_sends_but_not_a_persons(paths, gmail):
    mine = _approve(_job(paths))
    auto = _approve(ob.submit(_job(paths, origin="policy", policy_id="ack", reply_to_msg="m9")["job_id"]))
    ob.set_pause("auto")
    run(ob.tick())
    assert ob.require(mine["job_id"])["state"] == ob.SENT
    assert ob.require(auto["job_id"])["state"] == ob.SCHEDULED  # inspectable, cancellable
    ob.cancel(auto["job_id"])
    ob.set_pause("all")
    other = _approve(_job(paths))
    run(ob.tick())
    assert ob.require(other["job_id"])["state"] == ob.SCHEDULED
    ob.set_pause("")
    run(ob.tick())
    assert ob.require(other["job_id"])["state"] == ob.SENT


def test_an_unreadable_pause_value_fails_closed(paths, gmail):
    ob._store(None).meta_set(ob.PAUSE_KEY, "garbage")
    job = _approve(_job(paths))
    run(ob.tick())
    assert ob.require(job["job_id"])["state"] == ob.SCHEDULED


# ── revalidation ───────────────────────────────────────────────────────────


def test_a_disconnected_account_stops_its_pending_sends(paths, gmail):
    job = _approve(_job(paths))
    ob.disable_account("box@gmail.com")
    run(ob.tick())
    assert gmail.sent_messages == [] and ob.require(job["job_id"])["state"] == ob.NEEDS_REVIEW


def test_an_alias_removed_after_approval_is_not_used(paths, gmail):
    job = _approve(_job(paths))
    (paths.base / "accounts.yaml").write_text(ACCOUNTS.replace("support@studio.test", "gone@studio.test"),
                                              encoding="utf-8")
    run(ob.tick())
    assert gmail.sent_messages == [] and ob.require(job["job_id"])["state"] == ob.NEEDS_REVIEW


def test_a_send_far_past_its_time_goes_to_a_person(paths, gmail):
    job = _approve(_job(paths))
    run(ob.tick(now=ob.now_utc() + ob.MAX_LATE + timedelta(hours=1)))
    assert gmail.sent_messages == [] and ob.require(job["job_id"])["state"] == ob.NEEDS_REVIEW


def test_a_revalidator_that_crashes_blocks_the_send(paths, gmail, monkeypatch):
    def boom(_job):
        raise RuntimeError("policy store unreadable")

    monkeypatch.setattr(ob, "REVALIDATORS", [boom])
    job = _approve(_job(paths))
    run(ob.tick())
    assert gmail.sent_messages == [] and "safety check failed" in ob.require(job["job_id"])["last_error"]


# ── provider outcomes (scenario I) ─────────────────────────────────────────


def test_a_refusal_before_acceptance_fails_without_retrying(paths, gmail, mailroom_events):
    from navig_email.errors import ConnectorAPIError

    gmail.send_mode = ConnectorAPIError("gmail", 400, "invalid To")
    job = _approve(_job(paths))
    run(ob.tick())
    assert ob.require(job["job_id"])["state"] == ob.FAILED
    assert any(ev == "failed" for ev, *_ in mailroom_events)
    with pytest.raises(ob.OutboxError):
        ob.approve(job["job_id"], content_hash_seen=job["content_hash"])  # retry = reopen + re-approve


def test_a_rate_limit_is_retried_later_because_nothing_left(paths, gmail):
    from navig_email.errors import ConnectorAPIError

    gmail.send_mode = ConnectorAPIError("gmail", 429, "slow down")
    job = _approve(_job(paths))
    run(ob.tick())
    row = ob.require(job["job_id"])
    assert row["state"] == ob.SCHEDULED and ob.parse_iso(row["not_before"]) > ob.now_utc()
    gmail.send_mode = None
    run(ob.tick(now=ob.now_utc() + ob.RETRY_BACKOFF + timedelta(seconds=5)))
    assert ob.require(job["job_id"])["state"] == ob.SENT and len(gmail.sent_messages) == 1


def test_a_lost_response_is_reconciled_from_sent_not_resent(paths, gmail, mailroom_events):  # scenario I
    gmail.send_mode = "lost"
    job = _approve(_job(paths))
    run(ob.tick())  # the send times out; the same pass already finds it in Sent
    row = ob.require(job["job_id"])
    assert row["state"] == ob.SENT and row["provider_msg_id"] == "s1"
    chain = [e["to_state"] for e in ob.events(job["job_id"])]
    assert chain[-2:] == [ob.OUTCOME_UNKNOWN, ob.SENT]
    assert [ev for ev, *_ in mailroom_events][-2:] == ["outcome_unknown", "sent"]
    gmail.send_mode = None
    run(ob.tick(now=ob.now_utc() + timedelta(minutes=2)))
    assert len(gmail.sent_messages) == 1  # exactly one copy went out


def test_unknown_and_not_in_sent_goes_to_a_person_never_a_resend(paths, gmail):
    import httpx

    gmail.send_mode = httpx.ReadTimeout("gone")  # raised before the fake files it: truly not sent
    job = _approve(_job(paths))
    run(ob.tick())
    assert ob.require(job["job_id"])["state"] == ob.OUTCOME_UNKNOWN
    gmail.send_mode = None
    t = ob.now_utc()
    for minutes in (2, 10, 20):
        run(ob.tick(now=t + timedelta(minutes=minutes)))
        assert ob.require(job["job_id"])["state"] == ob.OUTCOME_UNKNOWN
    run(ob.tick(now=t + ob.RECONCILE_WINDOW + timedelta(minutes=1)))
    assert ob.require(job["job_id"])["state"] == ob.NEEDS_REVIEW
    assert gmail.sent_messages == []  # nothing was ever retried automatically


def test_a_failed_sent_lookup_is_not_read_as_not_sent(paths, gmail, monkeypatch):
    async def broken(*_a, **_k):
        raise RuntimeError("search unavailable")

    monkeypatch.setattr(gmail, "iter_message_ids", broken)
    gmail.send_mode = "lost"
    job = _approve(_job(paths))
    run(ob.tick())
    run(ob.tick(now=ob.now_utc() + timedelta(minutes=2)))
    row = ob.require(job["job_id"])
    assert row["state"] == ob.OUTCOME_UNKNOWN and int(row["reconcile_tries"]) >= 1
    assert len(gmail.sent_messages) == 1


# ── concurrency and restarts (scenarios J, K) ──────────────────────────────


def test_two_dispatchers_racing_send_one_copy(paths, gmail):  # scenario J
    job = _approve(_job(paths))
    barrier = threading.Barrier(2)
    claims: list[list] = []

    def worker(name):
        barrier.wait()
        claims.append(ob.claim_due(worker=name))

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    won = [c for c in claims if c]
    assert len(won) == 1 and won[0][0]["job_id"] == job["job_id"]
    run(ob.dispatch(won[0][0]))
    run(ob.tick())
    assert len(gmail.sent_messages) == 1


def test_a_stale_claim_is_released_and_a_dead_send_is_checked_not_repeated(paths, gmail):  # scenario K
    a = _approve(_job(paths))
    b = _approve(_job(paths, body="Second message."))
    claimed = {j["job_id"]: j for j in ob.claim_due(worker="dead-process")}
    # b reached SENDING and the process died with the provider call in flight (it got through)
    ob._transition(ob._store(None), claimed[b["job_id"]], ob.SENDING, actor="test", sending_at=ob.iso(ob.now_utc()))
    gmail.send_mode = None
    run(gmail.send_raw(__import__("navig_email.providers", fromlist=["x"]).build_mime(ob.require(b["job_id"])).as_bytes(),
                       thread_id="t1"))
    later = ob.now_utc() + ob.CLAIM_STALE + timedelta(minutes=1)
    run(ob.tick(now=later))
    assert ob.require(a["job_id"])["state"] == ob.SENT  # released, then sent normally
    run(ob.tick(now=later + timedelta(minutes=2)))
    assert ob.require(b["job_id"])["state"] == ob.SENT  # found in Sent
    sent_ids = [m["msg"]["X-Navig-Job"] for m in gmail.sent_messages]
    assert sent_ids.count(b["job_id"]) == 1 and sent_ids.count(a["job_id"]) == 1


def test_every_transition_is_recorded(paths, gmail):
    job = _approve(_job(paths))
    run(ob.tick())
    chain = [(e["from_state"], e["to_state"]) for e in ob.events(job["job_id"])]
    assert chain == [("", "DRAFT"), ("DRAFT", "PENDING_APPROVAL"), ("PENDING_APPROVAL", "SCHEDULED"),
                     ("SCHEDULED", "CLAIMED"), ("CLAIMED", "SENDING"), ("SENDING", "SENT")]


def test_the_transition_table_has_no_path_from_draft_to_sending_without_approval():
    # Breadth-first from DRAFT avoiding SCHEDULED (the only state approval produces) must not reach SENDING.
    seen, todo = {ob.DRAFT}, [ob.DRAFT]
    while todo:
        for nxt in ob.TRANSITIONS[todo.pop()]:
            if nxt not in seen and nxt != ob.SCHEDULED:
                seen.add(nxt)
                todo.append(nxt)
    assert ob.SENDING not in seen and ob.SENT not in seen


def test_reopen_needs_a_fresh_approval(paths, gmail):
    from navig_email.errors import ConnectorAPIError

    gmail.send_mode = ConnectorAPIError("gmail", 400, "bad")
    job = _approve(_job(paths))
    run(ob.tick())
    again = ob.reopen(job["job_id"])
    assert again["state"] == ob.PENDING_APPROVAL and again["approved_hash"] == ""


def test_parse_when():
    now = ob.now_utc()
    assert ob.parse_when("15m", now=now) - now == timedelta(minutes=15)
    assert ob.parse_when("now", now=now) == now
    with pytest.raises(ob.OutboxError):
        ob.parse_when("soonish")


def test_the_audit_trail_has_no_bodies_and_masks_addresses(paths, gmail):
    job = _approve(_job(paths, body="SECRET-BODY-TEXT"))
    run(ob.tick())
    rows = ob._store(None).audit_rows()
    text = " ".join(r["summary"] for r in rows)
    assert "SECRET-BODY-TEXT" not in text and "ann@client.test" not in text and "a***@client.test" in text
    assert {r["action"] for r in rows} >= {"outbox.create", "outbox.approve", "outbox.sent"}
    assert job["job_id"] in {r["ref"] for r in rows}


def test_raw_message_helper_still_builds_inbox_mail():
    # The fake's raw_message is shared with the send path; guard its shape.
    m = raw_message("x", thread="t", frm="a@b.c", to="d@e.f", subject="s", date="2026-10-16T09:00:00",
                    labels=["INBOX"], body="hi")
    assert m["payload"]["headers"][0]["value"] == "a@b.c"
