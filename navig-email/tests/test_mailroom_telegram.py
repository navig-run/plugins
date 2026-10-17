"""Mailroom buttons on Telegram: opaque tokens, single use, expiry, chat binding, confirm-to-send."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from fake_gmail import FakeGmail
from navig_email import outbox as ob, tg
from navig_email.space import MailroomPaths

ACCOUNTS = """
version: 1
accounts:
  - id: studio
    address: box@gmail.com
"""


class FakeChannel:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def _api_call(self, method, payload):
        self.calls.append((method, payload))
        return {"ok": True}

    def answers(self) -> list[str]:
        return [p.get("text", "") for m, p in self.calls if m == "answerCallbackQuery"]

    def sent(self) -> list[dict]:
        return [p for m, p in self.calls if m == "sendMessage"]


@pytest.fixture
def paths(tmp_path) -> MailroomPaths:
    root = tmp_path / "space"
    (root / ".navig").mkdir(parents=True)
    (root / "mailroom").mkdir(parents=True)
    (root / ".navig" / "config.yaml").write_text("mailroom: {}\n", encoding="utf-8")
    (root / "mailroom" / "accounts.yaml").write_text(ACCOUNTS, encoding="utf-8")
    return MailroomPaths(root)


@pytest.fixture
def box(monkeypatch) -> FakeGmail:
    fake = FakeGmail("box@gmail.com")

    async def factory(_a=None):
        return fake

    monkeypatch.setattr(ob, "CONNECTOR_FACTORY", factory)
    monkeypatch.setattr(ob, "ACCOUNT_CHECK", lambda _a: True)
    return fake


def scheduled(paths, delay=timedelta(minutes=15)):
    job = ob.create(account="box@gmail.com", to="ann@client.test", subject="Re: hi", body="Hello Ann, thanks.",
                    paths=paths, thread_id="t1")
    return ob.approve(job["job_id"], content_hash_seen=job["content_hash"], send_at=ob.now_utc() + delay)


def press(channel, data, chat=42):
    cq = {"id": "q", "data": data, "message": {"chat": {"id": chat}, "message_id": 1}, "from": {"id": chat}}
    asyncio.run(tg.handle(channel, cq, data))


def _find(rows, text):
    return next(b["callback_data"] for row in rows for b in row if b["text"].startswith(text) and "callback_data" in b)


def test_a_button_carries_only_an_opaque_token(paths, box):
    rows = tg.outbox_keyboard(scheduled(paths), "scheduled")
    assert rows and all(rows)
    for row in rows:
        for b in row:
            data = b["callback_data"]
            assert data.startswith("mr:") and len(data.encode()) <= 64
            assert "box@gmail.com" not in data and "ann" not in data


def test_buttons_follow_the_state(paths, box):
    sch = scheduled(paths)
    texts = [b["text"] for row in tg.outbox_keyboard(sch, "scheduled") for b in row]
    assert texts == ["View", "Send now", "Delay 1 h", "Cancel"]
    pending = ob.submit(ob.create(account="box@gmail.com", to="b@client.test", subject="s", body="Hello there.",
                                  paths=paths)["job_id"])
    texts = [b["text"] for row in tg.outbox_keyboard(pending, "pending_approval") for b in row]
    assert texts == ["View", "Approve · send in 15 min", "Cancel"]
    ob.cancel(sch["job_id"])
    assert [b["text"] for row in tg.outbox_keyboard(ob.require(sch["job_id"]), "x") for b in row] == []


def test_cancel_once_then_already_done(paths, box):
    job = scheduled(paths)
    data = _find(tg.outbox_keyboard(job, "scheduled"), "Cancel")
    ch = FakeChannel()
    press(ch, data)
    press(ch, data)
    assert ob.require(job["job_id"])["state"] == ob.CANCELLED
    assert ch.answers()[0].startswith("Cancelled") and ch.answers()[1].startswith("Already done")
    asyncio.run(ob.tick(now=ob.now_utc() + timedelta(hours=1)))
    assert box.sent_messages == []


def test_send_now_needs_a_confirming_tap_and_sends_once(paths, box):
    job = scheduled(paths)
    ch = FakeChannel()
    press(ch, _find(tg.outbox_keyboard(job, "scheduled"), "Send now"))
    assert box.sent_messages == [] and ch.sent(), "a confirmation message must come first"
    confirm = _find(ch.sent()[-1]["reply_markup"]["inline_keyboard"], "Yes, send it now")
    press(ch, confirm)
    press(ch, confirm)  # a double tap
    assert len(box.sent_messages) == 1 and ob.require(job["job_id"])["state"] == ob.SENT
    assert "confirmed" in ch.answers()[1] and ch.answers()[2].startswith("Already done")


def test_the_confirm_button_is_bound_to_its_chat(paths, box):
    job = scheduled(paths)
    ch = FakeChannel()
    press(ch, _find(tg.outbox_keyboard(job, "scheduled"), "Send now"), chat=42)
    confirm = _find(ch.sent()[-1]["reply_markup"]["inline_keyboard"], "Yes, send it now")
    press(ch, confirm, chat=99)
    assert "another chat" in ch.answers()[-1] and box.sent_messages == []


def test_an_expired_or_unknown_button_does_nothing(paths, box):
    job = scheduled(paths)
    data = tg.make("ob.cancel", {"job": job["job_id"]}, ttl=timedelta(seconds=-1))
    ch = FakeChannel()
    press(ch, data)
    press(ch, "mr:doesnotexist")
    assert ob.require(job["job_id"])["state"] == ob.SCHEDULED
    assert "expired" in ch.answers()[0] and "no longer valid" in ch.answers()[1]


def test_approve_from_telegram_refuses_a_text_edited_since(paths, box):
    job = ob.submit(ob.create(account="box@gmail.com", to="b@client.test", subject="s", body="Hello there.",
                              paths=paths)["job_id"])
    data = _find(tg.outbox_keyboard(job, "pending_approval"), "Approve")
    ob.edit(job["job_id"], body="Hello there, edited after the notification.")
    ch = FakeChannel()
    press(ch, data)
    assert ob.require(job["job_id"])["state"] == ob.PENDING_APPROVAL
    assert ch.answers()[0].startswith("Not done")


def test_view_shows_the_exact_text(paths, box):
    job = scheduled(paths)
    ch = FakeChannel()
    press(ch, _find(tg.outbox_keyboard(job, "scheduled"), "View"))
    assert "Hello Ann, thanks." in ch.sent()[0]["text"]


def test_delay_moves_the_send(paths, box):
    job = scheduled(paths)
    before = ob.parse_iso(job["not_before"])
    ch = FakeChannel()
    press(ch, _find(tg.outbox_keyboard(job, "scheduled"), "Delay"))
    assert ob.parse_iso(ob.require(job["job_id"])["not_before"]) - before >= timedelta(minutes=59)


def test_pause_button_on_an_automatic_reply(paths, box):
    job = ob.create(account="box@gmail.com", to="b@client.test", subject="s", body="Hello there.", paths=paths,
                    origin="policy", policy_id="business-ack", reply_to_msg="m1")
    job = ob.approve(job["job_id"], content_hash_seen=job["content_hash"])
    rows = tg.outbox_keyboard(job, "scheduled")
    ch = FakeChannel()
    press(ch, _find(rows, "Pause automatic replies"))
    assert ob.pause_level() == "auto"


def test_save_and_dismiss_briefing_items(paths, box):
    from navig_email.store import get_store

    s = get_store()
    s._write("INSERT INTO digest_items (item_id, fingerprint, kind, title, first_seen) VALUES ('fp1','fp1','job','Lead QA','x')")
    save, dismiss = (b["callback_data"] for b in tg.item_buttons("fp1", "job"))
    ch = FakeChannel()
    press(ch, save)
    assert s._read_one("SELECT status FROM digest_items WHERE item_id='fp1'")[0] == "saved"
    press(ch, dismiss)
    assert s._read_one("SELECT status FROM digest_items WHERE item_id='fp1'")[0] == "dismissed"


def test_notifications_carry_action_buttons_when_the_core_routes_them(paths, box):
    from navig_email import mail_notify

    job = scheduled(paths)
    kb = mail_notify._keyboard(job, "scheduled")
    flat = [b["text"] for row in kb for b in row]
    assert flat[:4] == ["View", "Send now", "Delay 1 h", "Cancel"] and flat[-1] == "Open thread"


def test_every_token_press_is_audited(paths, box):
    job = scheduled(paths)
    ch = FakeChannel()
    press(ch, _find(tg.outbox_keyboard(job, "scheduled"), "Cancel"))
    from navig_email.store import get_store

    actions = [r["action"] for r in get_store().audit_rows()]
    assert "telegram.ob.cancel" in actions
