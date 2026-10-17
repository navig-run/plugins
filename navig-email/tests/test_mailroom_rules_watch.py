"""Rules v1 + the incremental watch, against the in-memory Gmail."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # this checkout's plugin
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_gmail import FakeGmail, raw_message  # noqa: E402
from navig_email import actions as A, rules as R, state as S, watch as W  # noqa: E402
from navig_email.space import MailroomPaths  # noqa: E402


# ── rules ──────────────────────────────────────────────────────────────────


def _msg(**kw):
    base = {
        "from": "Bob <bob@spam.io>",
        "to": "support@cybesis.com",
        "cc": "",
        "subject": "Hello",
        "snippet": "",
        "body": "",
        "labels": ["INBOX", "UNREAD"],
        "has_attachment": False,
        "tags": [],
        "alias": "",
    }
    base.update(kw)
    return base


class TestRules:
    def test_match_block_is_flattened_and_to_matches(self):
        rule = {
            "id": "support",
            "match": {"to": "support@cybesis.com"},
            "actions": ["label:Cybesis/Support", "notify:telegram"],
        }
        assert R.match(_msg(), rule)
        assert not R.match(_msg(to="me@gmail.com"), rule)
        acts = R.actions_for(rule)
        assert [str(a) for a in acts] == ["label:Cybesis/Support", "notify:telegram"]

    def test_from_any_subject_any_in_and_attachment(self):
        rule = {
            "match": {
                "from_any": ["caf.fr", "edf.fr"],
                "subject_any": ["apl", "loyer"],
            },
            "actions": ["label:X"],
        }
        assert R.match(_msg(**{"from": "noreply@caf.fr", "subject": "Votre APL"}), rule)
        assert not R.match(
            _msg(**{"from": "noreply@caf.fr", "subject": "Bonjour"}), rule
        )
        assert R.match(
            _msg(labels=["SPAM"]), {"match": {"in": "spam"}, "actions": ["star"]}
        )
        assert not R.match(
            _msg(labels=["INBOX"]), {"match": {"in": "spam"}, "actions": ["star"]}
        )
        assert R.match(
            _msg(has_attachment=True),
            {"match": {"has_attachment": True}, "actions": ["star"]},
        )

    def test_legacy_daemon_rule_still_matches_and_channels_mean_notify(self):
        rule = {"from": "spam.io", "channels": ["deck", "telegram"], "enabled": True}
        assert R.match(_msg(), rule)
        assert [str(a) for a in R.actions_for(rule)] == ["notify:telegram"]

    def test_no_condition_never_matches(self):
        assert not R.match(_msg(), {"actions": ["star"]})

    def test_action_parsing_and_validation(self):
        assert R.parse_action("notify") == R.Action("notify", "telegram")
        with pytest.raises(ValueError):
            R.parse_action("delete")
        with pytest.raises(ValueError):
            R.parse_action("run:rm -rf /")  # only navig verbs
        with pytest.raises(ValueError):
            R.parse_action("label:")
        problems = R.validate(
            [
                {"id": "a", "match": {"to": "x"}, "actions": ["nope"]},
                {"id": "a", "match": {}, "actions": ["star"]},
                {"id": "b", "match": {"to": "x"}, "actions": ["star"], "bogus": 1},
            ]
        )
        assert any("unknown action" in p for p in problems)
        assert any("duplicate id" in p for p in problems)
        assert any("no condition" in p for p in problems)
        assert any("unknown key" in p for p in problems)

    def test_render_run_fills_placeholders_per_token_without_shell(self):
        argv = R.render_run(
            'navig paperwork note --from {from} --subject "{subject}" --id {id}',
            {
                "id": "m1",
                "from": "Bob <bob@x.io>",
                "subject": "a; rm -rf /",
                "thread_id": "t",
            },
        )
        assert argv == [
            "navig",
            "paperwork",
            "note",
            "--from",
            "bob@x.io",
            "--subject",
            "a; rm -rf /",
            "--id",
            "m1",
        ]


# ── watch ──────────────────────────────────────────────────────────────────


RULES = [
    {
        "id": "support",
        "name": "Support",
        "match": {"to": "support@cybesis.com"},
        "actions": ["label:Cybesis/Support", "notify:telegram"],
    },
    {
        "id": "caf",
        "match": {"from_any": ["caf.fr"]},
        "actions": ["label:Paperwork/Logement"],
    },
]


@pytest.fixture
def space(tmp_path) -> MailroomPaths:
    root = tmp_path / "space"
    (root / ".navig").mkdir(parents=True)
    return MailroomPaths(root)


@pytest.fixture
def box(monkeypatch):
    fake = FakeGmail("me@gmail.com")
    fake.add(
        raw_message(
            "old1",
            thread="t0",
            frm="a@b.c",
            to="me@gmail.com",
            subject="old",
            date="2026-09-01T10:00:00",
            labels=["INBOX"],
        )
    )
    import navig_email.account as acct

    async def _connector(account=None):
        return fake

    monkeypatch.setattr(acct, "connector", _connector)
    return fake


def _notify(monkeypatch, result=True):
    sent: list[str] = []
    import navig.messaging.notify_operator as no

    def fake(text, **kw):
        sent.append(text)
        return result

    monkeypatch.setattr(no, "notify_operator", fake)
    return sent


def test_first_pass_seeds_without_acting(space, box, monkeypatch):
    sent = _notify(monkeypatch)
    res = asyncio.run(W.run(space, RULES, dry_run=False))
    assert res.seeded and res.candidates == 1
    st = S.load(space)
    assert (
        st["seeded"]
        and st["history_id"] == str(box.history_id)
        and "old1" in st["seen_ids"]
    )
    assert sent == [] and not any(c[0] == "modify" for c in box.calls)


def test_second_pass_applies_rules_and_notifies_once(space, box, monkeypatch):
    sent = _notify(monkeypatch)
    asyncio.run(W.run(space, RULES, dry_run=False))
    box.add(
        raw_message(
            "m1",
            thread="t1",
            frm="Client <c@x.io>",
            to="support@cybesis.com",
            subject="Need help <urgent>",
            date="2026-09-02T10:00:00",
            labels=["INBOX", "UNREAD"],
        )
    )
    box.add(
        raw_message(
            "m2",
            thread="t2",
            frm="noreply@caf.fr",
            to="me@gmail.com",
            subject="Votre dossier",
            date="2026-09-02T11:00:00",
            labels=["INBOX", "UNREAD"],
        )
    )
    box.add(
        raw_message(
            "m3",
            thread="t3",
            frm="x@y.z",
            to="me@gmail.com",
            subject="nothing",
            date="2026-09-02T12:00:00",
            labels=["INBOX"],
        )
    )
    res = asyncio.run(W.run(space, RULES, dry_run=False))
    assert res.source == "history" and res.new == 3 and res.matched == 2
    assert res.outcome.labelled == 2 and res.notified is True and res.cursor_advanced
    assert len(sent) == 1 and "Support" in sent[0] and "&lt;urgent&gt;" in sent[0]
    assert "Label_7" in box.messages["m1"]["labelIds"] or any(
        box.labels[lid]["name"] == "Cybesis/Support"
        for lid in box.messages["m1"]["labelIds"]
        if lid in box.labels
    )
    st = S.load(space)
    assert {"m1", "m2", "m3"} <= set(st["seen_ids"]) and st["history_id"] == str(
        box.history_id
    )
    assert "Cybesis/Support" in st["label_ids"]

    # Nothing new → nothing happens, nothing sent.
    res = asyncio.run(W.run(space, RULES, dry_run=False))
    assert res.new == 0 and res.notified is None and len(sent) == 1


def test_failed_notification_keeps_cursor_for_retry(space, box, monkeypatch):
    asyncio.run(W.run(space, RULES, dry_run=False))
    cursor = S.load(space)["history_id"]
    box.add(
        raw_message(
            "m1",
            thread="t1",
            frm="c@x.io",
            to="support@cybesis.com",
            subject="Help",
            date="2026-09-02T10:00:00",
            labels=["INBOX"],
        )
    )
    _notify(monkeypatch, result=False)
    res = asyncio.run(W.run(space, RULES, dry_run=False))
    assert res.notified is False and not res.cursor_advanced
    st = S.load(space)
    assert st["history_id"] == cursor and "m1" not in st["seen_ids"]
    assert any("Telegram" in e for e in res.outcome.errors)


def test_stale_cursor_falls_back_to_window(space, box, monkeypatch):
    _notify(monkeypatch)
    asyncio.run(W.run(space, RULES, dry_run=False))
    box.add(
        raw_message(
            "m1",
            thread="t1",
            frm="noreply@caf.fr",
            to="me@gmail.com",
            subject="APL",
            date="2026-09-02T10:00:00",
            labels=["INBOX"],
        )
    )
    box.stale_history = True
    res = asyncio.run(W.run(space, RULES, dry_run=False))
    assert res.source == "window" and res.new == 1 and res.outcome.labelled == 1


def test_dry_run_changes_nothing(space, box, monkeypatch):
    sent = _notify(monkeypatch)
    asyncio.run(W.run(space, RULES, dry_run=False))
    box.add(
        raw_message(
            "m1",
            thread="t1",
            frm="c@x.io",
            to="support@cybesis.com",
            subject="Help",
            date="2026-09-02T10:00:00",
            labels=["INBOX"],
        )
    )
    before = dict(S.load(space))
    res = asyncio.run(W.run(space, RULES, dry_run=True))
    assert res.dry_run and res.matched == 1 and res.outcome.planned
    assert sent == [] and not any(c[0] == "modify" for c in box.calls)
    assert S.load(space)["seen_ids"] == before["seen_ids"]


def test_notification_text_batches_and_caps():
    lines = [f"• line {i}" for i in range(25)]
    text = A.notification_text(lines, title="T")
    assert text.startswith("📧 <b>T</b> — 25 message(s)") and "… +5" in text
    assert A.notification_text([], title="T") == ""


def test_run_action_executes_off_the_loop_and_reports_exit(monkeypatch):
    """`run:` goes through asyncio.create_subprocess_exec (never blocks the daemon loop)
    and decodes the child's bytes with the console helper."""
    calls: list[list[str]] = []

    class _Proc:
        def __init__(self, code, raw):
            self.returncode = code
            self._raw = raw

        async def communicate(self):
            return self._raw, b""

        def kill(self):
            pass

    async def fake_exec(*argv, **kw):
        calls.append(list(argv))
        return _Proc(0 if argv[1] == "ok" else 3, "caf\xe9 — ok".encode("utf-8"))

    monkeypatch.setattr(A.asyncio, "create_subprocess_exec", fake_exec)
    out = A.Outcome()
    msg = {"id": "m1", "thread_id": "t", "from": "a@b.c", "subject": "s"}
    rule = {"id": "r", "actions": ["run:navig ok --id {id}", "run:navig bad"]}
    acts = R.actions_for(rule)
    asyncio.run(
        A.apply(
            None, msg, rule, acts, labels=A.LabelCache(None), dry_run=False, out=out
        )
    )
    assert calls[0] == ["navig", "ok", "--id", "m1"]
    assert (
        out.ran == 1
        and len(out.errors) == 1
        and "exit 3" in out.errors[0]
        and "caf\xe9" in out.errors[0]
    )

    out2 = A.Outcome()
    asyncio.run(
        A.apply(
            None, msg, rule, acts, labels=A.LabelCache(None), dry_run=True, out=out2
        )
    )
    assert out2.ran == 0 and len(calls) == 2 and len(out2.planned) == 2


# ── edge tags (X-Cybesis-Tag / X-Cybesis-Alias) ────────────────────────────


def test_edge_tags_parsing():
    from navig_email.messages import edge_tags

    assert edge_tags("support,echeance, Facture ,support") == [
        "support",
        "echeance",
        "facture",
    ]
    assert edge_tags("") == [] and edge_tags(None) == []


def test_shape_exposes_the_edge_headers():
    from navig_email.messages import shape

    raw = raw_message(
        "m1",
        thread="t",
        frm="a@b.c",
        to="support@cybesis.com",
        subject="s",
        date="2026-09-20T10:00:00",
        labels=["INBOX"],
    )
    raw["payload"]["headers"] += [
        {"name": "X-Cybesis-Tag", "value": "support,facture"},
        {"name": "X-Cybesis-Alias", "value": "Support"},
    ]
    msg = shape(raw)
    assert msg["tags"] == ["support", "facture"] and msg["alias"] == "support"
    plain = shape(
        raw_message(
            "m2",
            thread="t",
            frm="a@b.c",
            to="me@gmail.com",
            subject="s",
            date="2026-09-20T10:00:00",
            labels=["INBOX"],
        )
    )
    assert plain["tags"] == [] and plain["alias"] == ""


def test_rules_match_on_edge_tag_and_alias():
    tagged = _msg(tags=["support", "echeance"], alias="support")
    assert R.match(
        tagged, {"match": {"tag": "echeance"}, "actions": ["label:Paperwork/Echeances"]}
    )
    assert not R.match(tagged, {"match": {"tag": "facture"}, "actions": ["label:X"]})
    assert R.match(
        tagged, {"match": {"tag_any": ["facture", "echeance"]}, "actions": ["label:X"]}
    )
    assert R.match(
        tagged, {"match": {"alias": "Support"}, "actions": ["label:Cybesis/Support"]}
    )
    assert not R.match(tagged, {"match": {"alias": "billing"}, "actions": ["label:X"]})
    assert R.match(
        tagged, {"match": {"alias_any": ["billing", "support"]}, "actions": ["label:X"]}
    )
    # Mail that never passed the edge never matches an edge rule; conditions still AND.
    assert not R.match(_msg(), {"match": {"tag": "support"}, "actions": ["label:X"]})
    assert not R.match(
        tagged,
        {
            "match": {"tag": "support", "subject_contains": "nope"},
            "actions": ["label:X"],
        },
    )
    assert "tag" in R.CONDITION_KEYS and "alias_any" in R.CONDITION_KEYS
    assert (
        R.validate(
            [
                {
                    "id": "e",
                    "match": {"tag": "support"},
                    "actions": ["label:Cybesis/Support"],
                }
            ]
        )
        == []
    )
