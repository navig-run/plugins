"""`navig email reply --queue`, `send --schedule` and `outbox …` against the in-memory Gmail."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fake_gmail import FakeGmail, raw_message
from navig_email import llm_guard, outbox as ob
from navig_email.commands.email import email_app

runner = CliRunner()
ME = "box@gmail.com"

ACCOUNTS = """
version: 1
accounts:
  - id: studio
    address: box@gmail.com
    aliases:
      - { address: support@studio.test }
"""
IDENTITIES = """
version: 1
default: studio
identities:
  studio:
    display: Sam
    company: House Studio
    match:
      aliases: [support]
  private:
    display: Sam
    language: French
    signature: Sam Perso
    match:
      aliases: [sam]
"""


@pytest.fixture
def space(tmp_path) -> Path:
    root = tmp_path / "space"
    (root / ".navig").mkdir(parents=True)
    (root / "mailroom").mkdir()
    (root / ".navig" / "config.yaml").write_text("mailroom:\n  ai:\n    model: ollama:small\n", encoding="utf-8")
    (root / "mailroom" / "accounts.yaml").write_text(ACCOUNTS, encoding="utf-8")
    (root / "mailroom" / "identities.yaml").write_text(IDENTITIES, encoding="utf-8")
    return root


@pytest.fixture
def box(monkeypatch):
    fake = FakeGmail(ME)
    msg = raw_message("m1", thread="t1", frm="Ann <ann@client.test>", to="support@studio.test",
                      subject="Partnership", date="2026-10-15T09:00:00", labels=["INBOX"],
                      body="Hello, we would like to discuss a partnership with your studio. Ann",
                      message_id="<m1@client>")
    msg["payload"]["headers"].append({"name": "X-Cybesis-Alias", "value": "support"})
    fake.add(msg)
    import navig_email.account as acct

    async def _connector(account=None):
        return fake

    monkeypatch.setattr(acct, "connector", _connector)
    monkeypatch.setattr(acct, "connected_email", lambda *a: ME)
    monkeypatch.setattr(acct, "is_connected", lambda *a: True)
    monkeypatch.setattr(ob, "CONNECTOR_FACTORY", _connector)
    monkeypatch.setattr(ob, "ACCOUNT_CHECK", lambda _a: True)
    monkeypatch.setattr(llm_guard, "resolve", lambda **kw: llm_guard.Resolved("ollama", "small"))
    monkeypatch.setattr(
        llm_guard, "generate",
        lambda messages, **kw: ("Hello Ann, thank you for reaching out. Could you share more about the project?",
                                llm_guard.Resolved("ollama", "small")),
    )
    return fake


def _run(*args):
    return runner.invoke(email_app, list(args))


def _json(output: str) -> dict:
    return json.loads(output[output.index("{"):])


def test_reply_queue_waits_for_approval_and_uses_the_alias(space, box):
    res = _run("reply", "t1", "--space", str(space), "--queue", "--json")
    assert res.exit_code == 0, res.output
    data = _json(res.output)
    assert data["job_state"] == "PENDING_APPROVAL" and data["draft_id"] == ""
    job = ob.require(data["job_id"])
    assert job["from_addr"] == "support@studio.test" and job["identity"] == "studio"
    assert job["from_name"] == "Sam" and job["in_reply_to"] == "<m1@client>" and job["thread_id"] == "t1"
    assert box.drafts == []  # queued instead of a Gmail draft — one copy of the text, not two
    _run("outbox", "dispatch")
    assert box.sent_messages == []  # Mode A/B: nothing goes out unapproved


def test_approve_needs_the_version_then_schedules_and_cancel_stops_it(space, box):
    job_id = _json(_run("reply", "t1", "--space", str(space), "--queue", "--json").output)["job_id"]
    res = _run("outbox", "approve", job_id[:8])
    assert res.exit_code == 2 and "--version" in res.output
    res = _run("outbox", "approve", job_id[:8], "--version", "deadbeef0000")
    assert res.exit_code == 1 and "changed" in res.output
    full = ob.require(job_id)["content_hash"]
    res = _run("outbox", "approve", job_id[:8], "--version", full[:12], "--in", "15m")
    assert res.exit_code == 0 and "not sent yet" in res.output
    assert ob.require(job_id)["state"] == "SCHEDULED"
    listing = _run("outbox", "list", "--json")
    assert _json(listing.output)["jobs"][0]["job_id"] == job_id
    assert _run("outbox", "cancel", job_id[:8]).exit_code == 0
    assert _run("outbox", "send-now", job_id[:8], "--yes").exit_code == 1
    assert box.sent_messages == []


def test_reply_queue_yes_send_in_then_send_now(space, box):
    data = _json(_run("reply", "t1", "--space", str(space), "--queue", "--yes", "--send-in", "30m", "--json").output)
    assert data["job_state"] == "SCHEDULED"
    assert _run("outbox", "send-now", data["job_id"][:8]).exit_code == 2  # needs --yes
    res = _run("outbox", "send-now", data["job_id"][:8], "--yes")
    assert res.exit_code == 0 and "Sent to" in res.output, res.output
    assert len(box.sent_messages) == 1 and ob.require(data["job_id"])["state"] == "SENT"


def test_edit_resets_approval(space, box, tmp_path):
    data = _json(_run("reply", "t1", "--space", str(space), "--queue", "--yes", "--json").output)
    res = _run("outbox", "edit", data["job_id"][:8], "--body", "Hello Ann, happy to talk. When suits you?")
    assert res.exit_code == 0 and "approve the new version" in res.output
    assert ob.require(data["job_id"])["state"] == "PENDING_APPROVAL"


def test_send_schedule_queues_and_send_yes_goes_through_the_ledger(space, box):
    res = _run("send", "--to", "x@client.test", "--subject", "Hi", "--body", "Hello there friend.",
               "--schedule", "1h", "--yes")
    assert res.exit_code == 0 and "Not sent yet" in res.output
    assert box.sent_messages == []
    res = _run("send", "--to", "x@client.test", "--subject", "Hi", "--body", "Hello there friend.", "--yes")
    assert res.exit_code == 0 and "Sent to x@client.test" in res.output
    assert len(box.sent_messages) == 1
    assert box.sent_messages[0]["msg"]["X-Navig-Job"]  # the ledger stamped it


def test_send_from_another_accounts_alias_is_refused(space, box):
    res = _run("send", "--to", "x@client.test", "--subject", "Hi", "--body", "Hello there.",
               "--from", "ceo@elsewhere.test", "--space", str(space), "--yes")
    assert res.exit_code == 1 and "not an address of" in res.output
    assert box.sent_messages == []


def test_pause_and_resume(space, box):
    assert _run("outbox", "pause").exit_code == 0
    assert ob.pause_level() == "auto"
    _run("send", "--to", "x@client.test", "--subject", "Hi", "--body", "Hello there friend.",
         "--schedule", "1h", "--yes")
    assert "PAUSED" in _run("outbox", "list").output
    assert _run("outbox", "resume").exit_code == 0 and ob.pause_level() == ""


def test_history_shows_no_bodies(space, box):
    _run("send", "--to", "x@client.test", "--subject", "Hi", "--body", "UNIQUE-BODY-TEXT here.", "--yes")
    out = _json(_run("outbox", "history", "--json").output)
    assert out["audit"] and "UNIQUE-BODY-TEXT" not in json.dumps(out)
