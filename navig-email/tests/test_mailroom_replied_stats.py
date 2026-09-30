"""The spam-reply finder, the stats, the digest, the model guard — no network."""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_gmail import FakeGmail, raw_message  # noqa: E402
from navig_email import (
    digest as D,
    llm_guard,
    messages as M,
    replied as RP,
    stats as ST,
)  # noqa: E402


ME = "me@gmail.com"
SIGNED = "We have carefully considered your proposal.\n\nBest regards,\nCybesis Studios"


def _box() -> FakeGmail:
    box = FakeGmail(ME)
    # t1: spam original still present, I replied, they answered back → confirmed + answered
    box.add(
        raw_message(
            "s1",
            thread="t1",
            frm="SEO Guru <guru@seo.biz>",
            to=ME,
            subject="Rank #1 in 210 countries",
            date="2026-08-01T09:00:00",
            labels=["SPAM"],
            body="We reviewed your website.",
            message_id="<s1@seo>",
        )
    )
    box.add(
        raw_message(
            "r1",
            thread="t1",
            frm=ME,
            to="guru@seo.biz",
            subject="Re: Rank #1 in 210 countries",
            date="2026-08-01T10:00:00",
            labels=["SENT"],
            body=SIGNED,
            in_reply_to="<s1@seo>",
        )
    )
    box.add(
        raw_message(
            "a1",
            thread="t1",
            frm="guru@seo.biz",
            to=ME,
            subject="Re: Rank #1 in 210 countries",
            date="2026-08-02T10:00:00",
            labels=["INBOX"],
            body="Is this a yes?",
        )
    )
    # t2: original purged; my reply is first, In-Reply-To set, signed → likely, no answer
    box.add(
        raw_message(
            "r2",
            thread="t2",
            frm=ME,
            to="Lead Gen <sales@leads.co>",
            subject="Re: 10k leads",
            date="2026-07-15T10:00:00",
            labels=["SENT"],
            body=SIGNED,
            in_reply_to="<gone@leads>",
        )
    )
    # t3: normal conversation with a client → skipped
    box.add(
        raw_message(
            "c1",
            thread="t3",
            frm="client@good.com",
            to=ME,
            subject="Invoice",
            date="2026-08-05T09:00:00",
            labels=["INBOX"],
            body="Please find attached",
        )
    )
    box.add(
        raw_message(
            "r3",
            thread="t3",
            frm=ME,
            to="client@good.com",
            subject="Re: Invoice",
            date="2026-08-05T10:00:00",
            labels=["SENT"],
            body="Thanks!",
            in_reply_to="<c1@x>",
        )
    )
    # t4: original purged, my reply first but NOT signed → dropped from likely
    box.add(
        raw_message(
            "r4",
            thread="t4",
            frm=ME,
            to="x@y.z",
            subject="Re: hi",
            date="2026-08-06T10:00:00",
            labels=["SENT"],
            body="ok",
            in_reply_to="<zz@y>",
        )
    )
    # t5: too old for --since
    box.add(
        raw_message(
            "r5",
            thread="t5",
            frm=ME,
            to="old@spam.io",
            subject="Re: old",
            date="2023-01-01T10:00:00",
            labels=["SENT"],
            body=SIGNED,
            in_reply_to="<o@x>",
        )
    )
    return box


class TestReplied:
    def test_classify_thread_tiers(self):
        box = _box()
        t1 = [M.shape(m) for m in box.messages.values() if m["threadId"] == "t1"]
        rt = RP.classify_thread(t1, folder_label="SPAM", my_email=ME)
        assert (
            rt.tier == RP.TIER_CONFIRMED
            and rt.answered_back
            and rt.sender == "guru@seo.biz"
        )
        assert (
            rt.sender_domain == "seo.biz" and rt.subject == "Rank #1 in 210 countries"
        )
        assert rt.answer_at.startswith("2026-08-02")
        t3 = [M.shape(m) for m in box.messages.values() if m["threadId"] == "t3"]
        assert RP.classify_thread(t3, folder_label="SPAM", my_email=ME) is None
        t2 = [M.shape(m) for m in box.messages.values() if m["threadId"] == "t2"]
        assert (
            RP.classify_thread(t2, folder_label="SPAM", my_email=ME).tier
            == RP.TIER_LIKELY
        )

    def test_find_end_to_end(self):
        box = _box()
        found = asyncio.run(
            RP.find(box, since=date(2026, 1, 1), folder="spam", my_email=ME)
        )
        assert [r.thread_id for r in found] == ["t2", "t1"]
        by = {r.thread_id: r for r in found}
        assert by["t1"].tier == RP.TIER_CONFIRMED and by["t2"].tier == RP.TIER_LIKELY
        assert "Cybesis Studios" in by["t1"].my_reply_text
        assert by["t2"].sender == "sales@leads.co"

    def test_corpus_and_stats(self, tmp_path):
        box = _box()
        found = asyncio.run(
            RP.find(box, since=date(2026, 1, 1), folder="spam", my_email=ME)
        )
        rows = [r.to_row() for r in found]
        path = RP.write_corpus(rows, tmp_path / "corpus.md")
        body = path.read_text(encoding="utf-8")
        assert (
            "210 countries" in body
            and "ils ont répondu" in body
            and "Cybesis Studios" in body
        )
        st = RP.compute_stats(rows)
        assert (
            st["total"] == 2 and st["answered_back"] == 1 and st["answer_rate"] == 0.5
        )
        assert st["by_month"] == {"2026-07": 1, "2026-08": 1}
        md = RP.stats_markdown(st, ledger_name="piratebay")
        assert "| 2026-08 | 1 |" in md
        tg = RP.telegram_text(st, ledger_name="piratebay")
        assert "2 réponse(s)" in tg and "50 %" in tg


class TestStatsDigest:
    def test_period_bounds(self):
        p = ST.period_bounds("week", today=date(2026, 9, 19))
        assert p.since == date(2026, 9, 13) and p.until == date(2026, 9, 20)
        assert "after:" in p.query() and "before:" in p.query()
        c = ST.period_bounds("month", since=date(2026, 9, 1), until=date(2026, 9, 30))
        assert c.name == "custom" and c.until == date(2026, 10, 1)
        with pytest.raises(ValueError):
            ST.period_bounds("year")

    def test_compute_counts_labels_and_senders(self):
        box = _box()
        box.add(
            raw_message(
                "i1",
                thread="t9",
                frm="Client <c@x.io>",
                to="support@cybesis.com",
                subject="Help",
                date="2026-08-03T10:00:00",
                labels=["INBOX", "UNREAD"],
                attachment=True,
            )
        )
        lid = asyncio.run(box.ensure_label("Cybesis/Support"))
        asyncio.run(box.modify_message("i1", add=[lid]))
        rules = [
            {
                "id": "support",
                "match": {"to": "support@cybesis.com"},
                "actions": ["label:Cybesis/Support", "notify"],
            }
        ]
        per = ST.period_bounds(
            "custom", since=date(2026, 8, 1), until=date(2026, 8, 31)
        )
        st = asyncio.run(ST.compute(box, per, rule_list=rules))
        assert st["counts"]["sent"] == 3 and st["counts"]["spam"] == 1
        assert (
            st["counts"]["inbox"] == 3
            and st["counts"]["unread"] == 1
            and st["counts"]["attachments"] == 1
        )
        assert st["labels"] == {"Cybesis/Support": 1} and st["rules"] == {"support": 1}
        assert ("c@x.io", 1) in st["top_senders"] and st["per_day"]["2026-08-03"] == 1
        md = ST.render_markdown(st, account=ME)
        assert "| Envoyés | 3 |" in md and "Cybesis/Support" in md
        tg = ST.telegram_text(st)
        assert "envoyés <b>3</b>" in tg and "support 1" in tg
        dg = D.render_markdown(st, account=ME)
        assert "Digest mail" in dg and "Règles" in dg
        assert D.telegram_text(st, prose="x < y").endswith("x &lt; y")

    def test_llm_guard_refuses_cloud_unless_allowed(self, monkeypatch):
        cloud = llm_guard.Resolved("openai", "gpt")
        with pytest.raises(llm_guard.CloudRefused):
            llm_guard.ensure_allowed(cloud, allow_cloud=False)
        llm_guard.ensure_allowed(cloud, allow_cloud=True)
        llm_guard.ensure_allowed(
            llm_guard.Resolved("ollama", "llama3"), allow_cloud=False
        )
        assert llm_guard.resolve(model="ollama:llama3") == llm_guard.Resolved(
            "ollama", "llama3"
        )
        assert llm_guard.resolve(model="mistral").provider == "?"

    def test_prose_summary_goes_through_the_guard(self, monkeypatch):
        box = _box()
        per = ST.period_bounds("month", today=date(2026, 8, 20))
        # Patch the SOURCE module, not the re-export shim: `generate` and `resolve` are the
        # same objects in both, and `generate` looks `resolve` up in ITS own globals — so
        # patching `llm_guard.resolve` here changed nothing and the refusal never fired.
        from navig.llm import guard as real_guard

        monkeypatch.setattr(
            real_guard, "resolve", lambda **kw: real_guard.Resolved("openai", "gpt")
        )
        with pytest.raises(llm_guard.CloudRefused):
            asyncio.run(D.prose_summary(box, per))
        calls = {}

        def fake_generate(messages, *, mode, model=None, allow_cloud=False, **kw):
            calls["mode"] = mode
            calls["n"] = len(messages)
            return "- résumé", llm_guard.Resolved("ollama", "llama3")

        monkeypatch.setattr(llm_guard, "generate", fake_generate)
        text, provider = asyncio.run(D.prose_summary(box, per, allow_cloud=True))
        assert (
            text == "- résumé"
            and provider == "ollama:llama3"
            and calls["mode"] == "summarize"
        )
