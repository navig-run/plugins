"""`navig email` CLI against the in-memory Gmail: exit codes, files written, guards."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_gmail import FakeGmail, raw_message  # noqa: E402
from navig_email.commands.email import email_app  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

runner = CliRunner()
ME = "me@gmail.com"
SIGNED = (
    "Considering it made it considerably funnier.\n\nBest regards,\nCybesis Studios"
)

RULES_YAML = """version: 1
account: me@gmail.com
rules:
  - id: support
    name: Support
    match: { to: support@cybesis.com }
    actions: [label:Cybesis/Support, notify:telegram]
"""


@pytest.fixture
def space(tmp_path) -> Path:
    root = tmp_path / "space"
    (root / ".navig").mkdir(parents=True)
    (root / "mailroom").mkdir()
    (root / "mailroom" / "rules.yaml").write_text(RULES_YAML, encoding="utf-8")
    (root / ".navig" / "config.yaml").write_text(
        "mailroom:\n  style: mailroom/style.md\n", encoding="utf-8"
    )
    (root / "mailroom" / "style.md").write_text(
        "Reply calmly, sign Cybesis Studios.", encoding="utf-8"
    )
    return root


@pytest.fixture
def box(monkeypatch):
    fake = FakeGmail(ME)
    fake.add(
        raw_message(
            "s1",
            thread="t1",
            frm="guru@seo.biz",
            to=ME,
            subject="Rank #1",
            date="2026-08-01T09:00:00",
            labels=["SPAM"],
            body="We reviewed your website.",
            message_id="<s1@seo>",
        )
    )
    fake.add(
        raw_message(
            "r1",
            thread="t1",
            frm=ME,
            to="guru@seo.biz",
            subject="Re: Rank #1",
            date="2026-08-01T10:00:00",
            labels=["SENT"],
            body=SIGNED,
            in_reply_to="<s1@seo>",
        )
    )
    fake.add(
        raw_message(
            "a1",
            thread="t1",
            frm="guru@seo.biz",
            to=ME,
            subject="Re: Rank #1",
            date="2026-08-02T10:00:00",
            labels=["INBOX"],
            body="Is this a yes?",
            message_id="<a1@seo>",
        )
    )
    import navig_email.account as acct

    async def _connector(account=None):
        return fake

    monkeypatch.setattr(acct, "connector", _connector)
    monkeypatch.setattr(acct, "connected_email", lambda: ME)
    monkeypatch.setattr(acct, "is_connected", lambda: True)
    return fake


def _payload(output: str) -> dict:
    return json.loads(output[output.index("{") :])


def _run(*args):
    return runner.invoke(email_app, list(args))


def test_status_disconnected_exits_1(monkeypatch):
    import navig_email.account as acct

    monkeypatch.setattr(acct, "is_connected", lambda: False)
    monkeypatch.setattr(acct, "connected_email", lambda: "")
    res = _run("status")
    assert res.exit_code == 1 and "not connected" in res.output


def test_watch_requires_yes_or_dry_run(space, box):
    res = _run("watch", "--space", str(space))
    assert res.exit_code == 2


def test_rules_check_and_list(space, box):
    res = _run("rules", "check", "--space", str(space))
    assert res.exit_code == 0, res.output
    (space / "mailroom" / "rules.yaml").write_text(
        "rules:\n  - id: x\n    actions: [nope]\n", encoding="utf-8"
    )
    res = _run("rules", "check", "--space", str(space))
    assert res.exit_code == 1


def test_replied_writes_ledger_corpus_report_and_labels(space, box, monkeypatch):
    sent = []
    import navig.messaging.notify_operator as no

    monkeypatch.setattr(
        no, "notify_operator", lambda text, **kw: sent.append(text) or True
    )
    res = _run(
        "replied",
        "--space",
        str(space),
        "--since",
        "2026-01-01",
        "--apply-labels",
        "--send",
        "--json",
    )
    assert res.exit_code == 0, res.output
    p = _payload(res.output)
    assert (
        p["found"] == 1
        and p["confirmed"] == 1
        and p["answered_back"] == 1
        and p["labelled"] == 1
    )
    ledger = space / "mailroom" / "ledger" / "piratebay.jsonl"
    assert ledger.exists()
    row = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
    assert row["thread_id"] == "t1" and row["labels_applied"] == ["Cybesis/PirateBay"]
    assert (space / "mailroom" / "piratebay" / "corpus.md").exists()
    assert (space / "mailroom" / "reports" / "piratebay-stats.md").exists()
    assert any(
        box.labels[lid]["name"] == "Cybesis/PirateBay"
        for lid in box.messages["s1"]["labelIds"]
        if lid in box.labels
    )
    assert sent and "1 réponse(s)" in sent[0]

    # Second run: idempotent ledger.
    res = _run("replied", "--space", str(space), "--since", "2026-01-01", "--json")
    p = _payload(res.output)
    assert p["ledger_added"] == 0 and p["ledger_updated"] == 1
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 1


def test_followup_refuses_cloud_then_drafts_with_local(space, box, monkeypatch):
    _run("replied", "--space", str(space), "--since", "2026-01-01", "--json")
    from navig_email import llm_guard

    monkeypatch.setattr(
        llm_guard, "resolve", lambda **kw: llm_guard.Resolved("anthropic", "claude")
    )
    res = _run("followup", "--space", str(space), "--draft")
    assert res.exit_code == 1 and "allow-cloud" in res.output
    assert box.drafts == []

    monkeypatch.setattr(
        llm_guard, "resolve", lambda **kw: llm_guard.Resolved("ollama", "llama3")
    )
    monkeypatch.setattr(
        llm_guard,
        "generate",
        lambda messages, **kw: (
            "Good news. We found the meeting.",
            llm_guard.Resolved("ollama", "llama3"),
        ),
    )
    res = _run("followup", "--space", str(space), "--draft", "--json")
    assert res.exit_code == 0, res.output
    p = _payload(res.output)
    assert p["drafted"] == 1 and p["provider"] == "ollama:llama3"
    assert (
        len(box.drafts) == 1
        and box.drafts[0]["threadId"] == "t1"
        and box.drafts[0]["in_reply_to"] == "<a1@seo>"
    )
    assert box.drafts[0]["to"] == "guru@seo.biz" and box.drafts[0][
        "subject"
    ].startswith("Re:")
    row = json.loads(
        (space / "mailroom" / "ledger" / "piratebay.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert row["draft_id"] == "d1"

    # Already drafted → nothing more.
    res = _run("followup", "--space", str(space), "--draft", "--json")
    assert _payload(res.output)["drafted"] == 0


def test_stats_and_digest_write_reports(space, box, monkeypatch):
    res = _run(
        "stats",
        "--space",
        str(space),
        "--since",
        "2026-08-01",
        "--until",
        "2026-08-31",
        "--json",
    )
    assert res.exit_code == 0, res.output
    p = _payload(res.output)
    assert (
        p["counts"]["sent"] == 1
        and p["counts"]["spam"] == 1
        and Path(p["report"]).exists()
    )
    res = _run("digest", "--space", str(space), "--period", "month", "--json")
    assert res.exit_code == 0, res.output
    assert Path(_payload(res.output)["report"]).exists()


def test_search_list_read_and_tag(space, box):
    res = _run("search", "in:spam", "--json")
    assert (
        res.exit_code == 0
        and _payload(res.output.replace("[", '{"rows":[', 1) + "}")["rows"][0]["id"]
        == "s1"
    )
    res = _run("read", "r1", "--thread", "--json")
    assert res.exit_code == 0 and "Cybesis Studios" in res.output
    res = _run("tag", "s1", "--add", "Cybesis/PirateBay")
    assert res.exit_code == 0
    res = _run("tag", "--query", "in:spam", "--add", "X")
    assert res.exit_code == 2  # needs --yes


def test_send_needs_yes_and_draft_threads(space, box):
    res = _run("send", "--to", "a@b.c", "--subject", "Hi", "--body", "x")
    assert res.exit_code == 2
    res = _run(
        "send",
        "--to",
        "a@b.c",
        "--subject",
        "Hi",
        "--body",
        "x",
        "--draft",
        "--reply-to",
        "a1",
    )
    assert res.exit_code == 0, res.output
    assert (
        box.drafts[-1]["threadId"] == "t1"
        and box.drafts[-1]["in_reply_to"] == "<a1@seo>"
    )
