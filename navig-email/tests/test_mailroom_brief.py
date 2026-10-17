"""The newsletter briefing against real (sanitized) newsletters.

Scenario A (product newsletters merged, delivered, archived, never repeated), B (the GitHub
event, exact), C (three Free-Work jobs, salary ≠ day rate), H (a failed delivery cleans nothing
up), plus caching, DST, postponement, Trash/restore and deadline alerts.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from fake_gmail import FakeGmail, raw_message
from navig_email import cleanup as CL
from navig_email.brief import extract as X, render as R, run as RUN, schedule as SCH, synthesize as SY
from navig_email.brief.model import Item, Source
from navig_email.space import MailroomPaths

FIX = Path(__file__).parent / "fixtures" / "mail"
ME = "box@gmail.com"
NOW = datetime(2026, 10, 19, 7, 0, tzinfo=timezone.utc)  # Monday 09:00 Paris


def fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def add_fixture(box: FakeGmail, name: str, *, mid: str | None = None, date_iso: str = "2026-10-15T07:00:00",
                body: str | None = None, subject: str | None = None) -> str:
    f = fixture(name)
    m = raw_message(mid or f["id"], thread=mid or f["id"], frm=f["from"], to=f["to"], subject=subject or f["subject"],
                    date=date_iso, labels=["INBOX"], body=body if body is not None else f["body"],
                    message_id=f"<{mid or f['id']}@fixture>")
    for k, v in (f.get("headers") or {}).items():
        m["payload"]["headers"].append({"name": k, "value": v})
    box.add(m)
    return m["id"]


def shaped(name: str) -> dict:
    f = fixture(name)
    return {"id": f["id"], "from": f["from"], "subject": f["subject"], "date": "2026-10-15T07:00:00+00:00",
            "body": X.undouble(f["body"])}


def items_of(name: str) -> list[Item]:
    m = shaped(name)
    src = X.source_of(m, ME)
    for _n, detect, parse in X.PARSERS:
        if detect(m):
            got = parse(m, src)
            if got:
                return got
    return []


BRIEF_CFG = """mailroom:
  brief:
    frequency: weekly
    weekday: monday
    time: "08:30"
    timezone: Europe/Paris
    min_items: 3
    interests:
      "Color grading and media production": [color, grade, lut, footage]
      "Desktop automation and voice interfaces": [assistant, voice, automation, mac]
    jobs:
      keywords: [qa, ai, data, lead]
      locations: [Paris, Île-de-France]
      min_day_rate: 450
"""


@pytest.fixture
def paths(tmp_path) -> MailroomPaths:
    root = tmp_path / "space"
    (root / ".navig").mkdir(parents=True)
    (root / "mailroom").mkdir(parents=True)
    (root / ".navig" / "config.yaml").write_text(BRIEF_CFG, encoding="utf-8")
    return MailroomPaths(root)


@pytest.fixture
def box() -> FakeGmail:
    return FakeGmail(ME)


class Deliver:
    def __init__(self, ok: bool = True):
        self.ok, self.calls = ok, []

    async def __call__(self, msgs):
        self.calls.append(msgs)
        return (self.ok, "telegram", "" if self.ok else "telegram: 502 Bad Gateway")


def brief(box, paths, deliver, **kw):
    kw.setdefault("now", NOW)
    return asyncio.run(RUN.run(paths, connector=box, account=ME, deliver=deliver, use_model=False, **kw))


# ── stage 1 against the real newsletters ───────────────────────────────────


def test_product_hunt_items_are_extracted_separately_with_their_fields():
    got = {i.title: i for i in items_of("producthunt_2026-10-15")}
    assert {"Nostaliga", "CinemaLut", "Atticus"} <= set(got)
    cl = got["CinemaLut"]
    assert cl.summary == "Any frame's color, as a LUT for yours."
    assert cl.fields["category"] == "Video / Creative Tools" and cl.fields["platform"] == "Web"
    assert cl.links["primary"] == "https://www.producthunt.com/posts/cinemalut"  # tracking stripped
    assert cl.fields["published"] == "2026-10-15" and cl.fields["list"] == "editorial"
    assert any("LUT" in f or "grade" in f for f in cl.fields["features"])
    at = got["Atticus"]
    assert at.fields["category"] == "AI / Automation" and at.fields["platform"] == "macOS"
    assert at.summary.endswith("does what you asked")  # a tagline wrapped over two lines
    # never an invented platform: nothing in Nostaliga's text names one
    assert got["Nostaliga"].fields["platform"] == ""
    # ads are not products
    assert "Clarify" not in got and "Lightfield" not in got


def test_github_event_is_exact_and_not_marked_booked():  # scenario B
    [ev] = items_of("github_universe_paris")
    f = ev.fields
    assert ev.title == "GitHub Universe on the Road — Paris"
    assert (f["date"], f["weekday"], f["start"], f["end"]) == ("2026-11-30", "Monday", "09:00", "17:00")
    assert f["venue"] == "Comet Bourse" and f["address"] == "35 Rue Saint-Marc, 75002 Paris"
    assert f["timezone"] == "Europe/Paris" and "inferred" in f["timezone_source"]
    assert {"AI agents", "software delivery", "GitHub roadmap", "engineering leadership"} <= set(f["topics"])
    assert f["registration"].startswith("Not confirmed")
    assert {"register", "maps"} <= set(ev.links)
    texts = [b["text"] for b in R.buttons(ev)]
    assert texts == ["Register", "Maps", "Calendar"]
    cal = R.calendar_url(ev)
    assert "dates=20261130T090000%2F20261130T170000" in cal and "ctz=Europe%2FParis" in cal


def test_free_work_jobs_keep_salary_and_day_rate_apart():  # scenario C
    jobs = {i.title: i for i in items_of("freework_2026-10-01") if i.kind == "job"}
    assert set(jobs) == {"Lead QA", "Data engineer", "AI Tech Lead"}
    assert jobs["Lead QA"].fields["salary_year"] == {"min": 60000, "max": 70000, "currency": "EUR"}
    assert jobs["Lead QA"].fields["day_rate"] == {"min": 500, "max": 600, "currency": "EUR"}
    assert jobs["Lead QA"].fields["location"] == "Paris"
    assert jobs["Data engineer"].fields["salary_year"] == {"min": None, "max": 71000, "currency": "EUR"}
    assert jobs["Data engineer"].fields["day_rate"] == {"min": 400, "max": 550, "currency": "EUR"}
    assert jobs["Data engineer"].fields["location"] == "Île-de-France"
    assert jobs["AI Tech Lead"].fields["salary_year"] == {"min": 65000, "max": 70000, "currency": "EUR"}
    assert jobs["AI Tech Lead"].fields["day_rate"] == {"min": 465, "max": 700, "currency": "EUR"}
    # each link stays with its own job
    assert jobs["Lead QA"].links["apply"].endswith("/lead-automatisation-qa-f-h")
    assert jobs["AI Tech Lead"].links["apply"].endswith("/ai-tech-lead-genai-h-f")
    # editorial content is extracted, but separately from the offers
    assert any(i.kind == "article" for i in items_of("freework_2026-10-01"))


def test_job_rendering_says_year_and_day_separately():
    job = next(i for i in items_of("freework_2026-10-01") if i.title == "Lead QA")
    text = R.item_text(job)
    assert "€60k–€70k / year" in text and "€500–€600 / day" in text


def test_relevance_is_explained():
    interests = SY.Interests.from_config({"interests": {"Color grading and media production": ["lut", "footage"]},
                                          "jobs": {"keywords": ["qa"], "locations": ["Paris"], "min_day_rate": 450}})
    cl = next(i for i in items_of("producthunt_2026-10-15") if i.title == "CinemaLut")
    assert SY.score(cl, interests, today=date(2026, 10, 19)).reasons == ["Color grading and media production"]
    qa = next(i for i in items_of("freework_2026-10-01") if i.title == "Lead QA")
    reasons = SY.score(qa, interests, today=date(2026, 10, 19)).reasons
    assert "matches qa" in reasons and "in Paris" in reasons and any("≥ €450" in r for r in reasons)


def test_similar_but_distinct_items_stay_apart_and_the_same_one_merges():
    src_a, src_b = Source(ME, "a"), Source(ME, "b")
    j1 = Item("job", "Data Engineer", fields={"location": "Paris"}, links={"primary": "https://x.test/jobs/1"}, sources=[src_a])
    j2 = Item("job", "Data Engineer", fields={"location": "Lyon"}, links={"primary": "https://x.test/jobs/2"}, sources=[src_a])
    p1 = Item("product", "CinemaLut", links={"primary": "https://www.producthunt.com/posts/cinemalut?utm_source=a"}, sources=[src_a])
    p2 = Item("product", "CinemaLut", summary="longer summary", links={"primary": "https://www.producthunt.com/posts/cinemalut"}, sources=[src_b])
    merged = SY.merge([j1, j2, p1, p2])
    assert len(merged) == 3
    cl = next(i for i in merged if i.kind == "product")
    assert [s.message_id for s in cl.sources] == ["a", "b"] and cl.summary == "longer summary"


# ── scenario A: merged, delivered, archived, not repeated ───────────────────


def test_scenario_a_six_newsletters_one_briefing_no_duplicates(paths, box):
    ph = fixture("producthunt_2026-10-15")["body"]
    for i in range(5):  # five issues; the same three editorial picks recur in some of them
        add_fixture(box, "producthunt_2026-10-15", mid=f"ph{i}", date_iso=f"2026-10-1{i}T07:00:00", body=ph)
    add_fixture(box, "github_universe_paris", date_iso="2026-10-15T08:00:00")
    deliver = Deliver()
    res = brief(box, paths, deliver)
    assert res.status == "delivered", res.reason
    assert len(res.sources) == 6
    products = res.sections["product"]
    titles = [p.title for p in products]
    assert len(titles) == len(set(titles))  # merged, not repeated
    cinema = next(p for p in products if p.title == "CinemaLut")
    assert len(cinema.sources) == 5  # one item, every email it came from
    assert res.sections["event"][0].title == "GitHub Universe on the Road — Paris"
    assert len(deliver.calls) == 1
    # sources archived: out of the inbox, labelled, still in the mailbox
    assert res.cleanup and len(res.cleanup.archived) == 6
    for mid in res.cleanup.archived:
        labels = set(box.messages[mid]["labelIds"])
        assert "INBOX" not in labels and "TRASH" not in labels
    # a second run delivers nothing again
    again = brief(box, paths, deliver, now=NOW + timedelta(hours=1))
    assert again.status in ("empty", "not_due") and len(deliver.calls) == 1


def test_scenario_h_failed_delivery_cleans_nothing_up_and_retries_later(paths, box):
    add_fixture(box, "producthunt_2026-10-15")
    add_fixture(box, "freework_2026-10-01", date_iso="2026-10-14T07:00:00")
    res = brief(box, paths, Deliver(ok=False))
    assert res.status == "failed" and res.cleanup is None
    for m in box.messages.values():
        assert "INBOX" in m["labelIds"]
    from navig_email.store import get_store

    s = get_store()
    assert s._read_one("SELECT delivery_state FROM digest_runs")[0] == "failed"
    assert {r[0] for r in s._read_all("SELECT status FROM digest_items")} == {"new"}
    ok = Deliver()
    retry = brief(box, paths, ok, now=NOW + timedelta(minutes=30))
    assert retry.status == "delivered" and retry.item_count > 0


def test_cleanup_refuses_without_a_delivered_run(paths, box):
    res = asyncio.run(CL.after_delivery(box, [], account=ME, run_id="nope", cfg=CL.CleanupConfig()))
    assert res.errors and not res.archived


def test_protected_newsletters_are_kept(paths, box):
    ph = fixture("producthunt_2026-10-15")["body"]
    # deep in the body, so it is a source (the snippet is clean) but clean-up must still keep it
    add_fixture(box, "producthunt_2026-10-15", mid="inv", body=ph + "\n\nYour invoice is attached.")
    res = brief(box, paths, Deliver())
    assert res.status == "delivered"
    assert "inv" in res.cleanup.kept and "payment" in res.cleanup.kept["inv"]
    assert "INBOX" in box.messages["inv"]["labelIds"]


def test_trash_mode_retention_saved_items_and_restore(paths, box):
    (paths.space_root / ".navig" / "config.yaml").write_text(
        BRIEF_CFG + "    cleanup:\n      trash_senders: [digest.producthunt.com]\n      trash_after_days: 7\n",
        encoding="utf-8")
    add_fixture(box, "producthunt_2026-10-15", mid="ph1")
    add_fixture(box, "github_universe_paris")
    res = brief(box, paths, Deliver())
    assert res.cleanup.scheduled_trash == ["ph1"]  # only the opted-in sender
    early = asyncio.run(CL.trash_due(box, account=ME, now=NOW + timedelta(days=3)))
    assert early.trashed == []
    late = asyncio.run(CL.trash_due(box, account=ME, now=NOW + timedelta(days=8)))
    assert late.trashed == ["ph1"] and "TRASH" in box.messages["ph1"]["labelIds"]
    assert asyncio.run(CL.restore(box, account=ME, message_id="ph1"))
    labels = set(box.messages["ph1"]["labelIds"])
    assert "INBOX" in labels and "TRASH" not in labels


def test_a_saved_item_keeps_its_source_out_of_trash(paths, box):
    (paths.space_root / ".navig" / "config.yaml").write_text(
        BRIEF_CFG + "    cleanup:\n      trash_senders: [digest.producthunt.com]\n", encoding="utf-8")
    add_fixture(box, "producthunt_2026-10-15", mid="ph1")
    add_fixture(box, "github_universe_paris")
    brief(box, paths, Deliver())
    from navig_email.store import get_store

    get_store()._write("UPDATE digest_items SET status='saved' WHERE title='CinemaLut'")
    out = asyncio.run(CL.trash_due(box, account=ME, now=NOW + timedelta(days=8)))
    assert out.trashed == [] and "ph1" in out.kept


# ── schedule ───────────────────────────────────────────────────────────────


def test_weekly_slot_holds_its_wall_clock_across_dst():
    cfg = SCH.BriefConfig.from_config({"frequency": "weekly", "weekday": "monday", "time": "08:30",
                                       "timezone": "Europe/Paris"})
    before = SCH.last_slot(datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc), cfg, None)
    after = SCH.last_slot(datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc), cfg, None)  # DST ended 25 Oct
    assert before.astimezone(timezone.utc).hour == 6 and after.astimezone(timezone.utc).hour == 7
    assert before.astimezone(cfg.tz).strftime("%H:%M") == after.astimezone(cfg.tz).strftime("%H:%M") == "08:30"


def test_due_respects_slot_served_and_quiet_hours():
    cfg = SCH.BriefConfig.from_config({})
    monday_0900 = datetime(2026, 10, 19, 7, 0, tzinfo=timezone.utc)
    d = SCH.due(monday_0900, cfg, last_delivered=None, last_slot_served=None)
    assert d.due
    assert not SCH.due(monday_0900, cfg, last_delivered=None, last_slot_served=d.slot).due
    assert SCH.due(datetime(2026, 10, 19, 21, 30, tzinfo=timezone.utc), cfg, last_delivered=None,
                   last_slot_served=None).reason == "quiet hours"
    with pytest.raises(ValueError):
        SCH.BriefConfig.from_config({"frequency": "fortnightly-ish"})


def test_a_thin_week_is_postponed_but_never_beyond_the_cap(paths, box):
    from navig_email.store import get_store

    add_fixture(box, "github_universe_paris")  # one item < min_items 3
    deliver = Deliver()
    get_store().meta_set(RUN._meta_key(ME, "last_delivered"), RUN._iso(NOW - timedelta(days=3)))
    first = brief(box, paths, deliver, scheduled=True)
    assert first.status == "postponed" and deliver.calls == []
    # the slot stays open while postponed; once the last briefing is 14+ days old it goes out
    get_store().meta_set(RUN._meta_key(ME, "last_delivered"), RUN._iso(NOW - timedelta(days=15)))
    later = brief(box, paths, deliver, scheduled=True, now=NOW + timedelta(minutes=30))
    assert later.status == "delivered", later.reason


def test_the_very_first_briefing_is_not_held_back(paths, box):
    add_fixture(box, "github_universe_paris")
    res = brief(box, paths, Deliver(), scheduled=True)
    assert res.status == "delivered"


def test_scheduled_run_is_quiet_when_not_due(paths, box):
    add_fixture(box, "producthunt_2026-10-15")
    tuesday = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)
    from navig_email.store import get_store

    get_store().meta_set(RUN._meta_key(ME, "last_slot"), RUN._iso(datetime(2026, 10, 19, 6, 30, tzinfo=timezone.utc)))
    res = brief(box, paths, Deliver(), scheduled=True, now=tuesday)
    assert res.status == "not_due"


# ── cache & the model fallback ─────────────────────────────────────────────


def test_extraction_is_cached_by_content(box):
    msg = shaped("freework_2026-10-01")
    first = X.extract(msg, account=ME)
    calls = []
    second = X.extract(msg, account=ME, use_model=True, generate=lambda *a, **k: calls.append(1))
    assert [i.title for i in first] == [i.title for i in second] and calls == []


def test_model_fallback_drops_invented_links_and_numbers():
    msg = {"id": "n1", "from": "News <news@letter.test>", "subject": "Weekly", "date": "2026-10-15T07:00:00",
           "body": "This week: Foo launches a tool for bar. Read it at https://foo.test/launch. It costs 20 euros."}
    out = json.dumps([
        {"kind": "product", "title": "Foo", "summary": "a tool", "url": "https://foo.test/launch", "fields": {"price": "20"}},
        {"kind": "product", "title": "Invented", "summary": "x", "url": "https://evil.test/", "fields": {}},
        {"kind": "product", "title": "Wrong price", "summary": "x", "url": "", "fields": {"price": "999"}},
        {"kind": "spaceship", "title": "nope"},
    ])
    items = X.parse_with_model(msg, X.source_of(msg, ME), generate=lambda *a, **k: out)
    assert [i.title for i in items] == ["Foo"] and items[0].fields["extracted_by"] == "model"


def test_a_model_failure_is_not_cached_so_a_later_run_can_retry():
    msg = {"id": "n2", "from": "x <x@y.test>", "subject": "s", "body": "plain text with nothing parsable at all here"}

    def boom(*a, **k):
        raise RuntimeError("model down")

    assert X.extract(msg, account=ME, use_model=True, generate=boom) == []
    assert not X.is_cached(msg)


# ── deadline alerts ────────────────────────────────────────────────────────


def test_an_event_close_by_is_announced_once_on_its_own(paths, box):
    add_fixture(box, "github_universe_paris")
    add_fixture(box, "producthunt_2026-10-15")
    add_fixture(box, "freework_2026-10-01", date_iso="2026-10-14T07:00:00")
    brief(box, paths, Deliver())
    cfg = SCH.BriefConfig.from_config({})
    from navig_email.store import get_store

    s = get_store()
    d = Deliver()
    near = datetime(2026, 11, 28, 10, 0, tzinfo=timezone.utc)
    sent = asyncio.run(RUN.deadline_alerts(account=ME, cfg=cfg, now=near, store=s, deliver=d))
    assert sent == ["GitHub Universe on the Road — Paris"] and "Coming up" in d.calls[0][0][0]
    again = asyncio.run(RUN.deadline_alerts(account=ME, cfg=cfg, now=near + timedelta(hours=2), store=s, deliver=d))
    assert again == [] and len(d.calls) == 1


def test_rendered_messages_fit_telegram():
    items = items_of("producthunt_2026-10-15") * 10
    msgs = R.messages({"product": items}, title="T")
    assert all(len(text) <= 4096 for _t, text, _k in msgs)


# ── which emails may be briefing sources (found live: password resets were picked) ──


def _plain(box, mid, frm, subject, body, headers=None, date_iso="2026-10-15T09:00:00"):
    m = raw_message(mid, thread=mid, frm=frm, to=ME, subject=subject, date=date_iso, labels=["INBOX"], body=body)
    for k, v in (headers or {}).items():
        m["payload"]["headers"].append({"name": k, "value": v})
    box.add(m)


def test_transactional_and_security_mail_is_never_a_source(paths, box):
    from navig_email.store import get_store

    _plain(box, "pw", '"Threads" <no-reply@mail.threads.net>', "Reset your password", "Click to reset your password.")
    _plain(box, "st", "Stripe <notifications@stripe.com>", "Continue setting up your Stripe account", "Finish setup.")
    _plain(box, "sec", "News <news@letter.test>", "Your password has been changed", "Security notice.",
           headers={"List-Unsubscribe": "<https://letter.test/u>"})
    add_fixture(box, "freework_2026-10-01", date_iso="2026-10-01T08:00:00")
    srcs = asyncio.run(RUN.candidates(box, account=ME, cfg=SCH.BriefConfig.from_config({}), store=get_store()))
    assert [m["id"] for m in srcs] == ["1a0f60ef4dfd7014"]


def test_known_senders_come_first_and_sources_can_be_restricted(paths, box):
    from navig_email.store import get_store

    for i in range(7):
        _plain(box, f"n{i}", "Shop <deals@shop.test>", f"Deal {i}", "Big sale this week on everything.",
               headers={"List-Unsubscribe": "<https://shop.test/u>"}, date_iso=f"2026-10-1{i % 6}T10:00:00")
    add_fixture(box, "producthunt_2026-10-15", date_iso="2026-10-01T07:00:00")  # older, but a known sender
    srcs = asyncio.run(RUN.candidates(box, account=ME, cfg=SCH.BriefConfig.from_config({}), store=get_store()))
    assert srcs and srcs[0]["id"] == "1a13ff1f5680d595" and len(srcs) == 6
    only = asyncio.run(RUN.candidates(box, account=ME, cfg=SCH.BriefConfig.from_config({"sources": ["shop.test"]}),
                                      store=get_store()))
    assert only and {m["id"] for m in only} <= {f"n{i}" for i in range(7)}
