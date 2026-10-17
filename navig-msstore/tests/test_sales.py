"""Store sale detection — the part that decides when NAVIG OS rings.

Partner Center reports DAILY acquisition rows, so "a new sale" is a row whose quantity
grew since the last check. These pin that arithmetic, the first-check baseline (no ring
for history), the paid/free split (rows carry no type — only a price), the 429 pacing,
and that a locked state file stops a check instead of being overwritten.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from navig_msstore import sales as S
from navig_msstore.addons import DevCenterError

TODAY = dt.date(2026, 10, 4)
APP = {"id": "9MATRIX", "name": "Digital Rain Matrix Screensaver"}


def sale(qty: int, usd: float, date: str = "2026-10-04", product: str = "pro") -> S.Sale:
    return S.Sale(APP["id"], APP["name"], product, "Pro Upgrade", date, qty, usd)


def test_paid_rows_only_and_addon_names():
    rows = [
        {"date": "2026-10-04T00:00:00", "acquisitionQuantity": 3, "purchasePriceUSDAmount": 0},  # free downloads
        {"date": "2026-10-04T00:00:00", "acquisitionQuantity": 2, "purchasePriceUSDAmount": 12.98,
         "addonProductId": "pro", "inAppProductName": "Pro Upgrade"},
        {"date": "2026-10-03", "acquisitionQuantity": "x", "purchasePriceUSDAmount": 5},  # malformed → skipped
    ]
    got = S.paid_sales_from_rows(APP, "inappacquisitions", rows)
    assert got == [S.Sale(APP["id"], APP["name"], "pro", "Pro Upgrade", "2026-10-04", 2, 12.98)]
    paid_app = S.paid_sales_from_rows(APP, "appacquisitions", [{"date": "2026-10-04", "acquisitionQuantity": 1, "purchasePriceUSDAmount": 4.99}])
    assert paid_app[0].product_id == APP["id"] and paid_app[0].product_name == APP["name"]


def test_first_check_is_a_silent_baseline():
    new, state = S.diff_sales(S.empty_state(), [sale(5, 32.5)], today=TODAY)
    assert new == []                       # months of history must not ring
    assert state["seeded"] is True and state["seen"][sale(5, 32.5).key]["qty"] == 5


def test_only_the_growth_is_new_with_its_share_of_the_money():
    _, state = S.diff_sales(S.empty_state(), [sale(1, 6.5)], today=TODAY)
    new, state = S.diff_sales(state, [sale(3, 19.5)], today=TODAY)
    assert [(s.quantity, s.usd) for s in new] == [(2, 13.0)]
    new, _ = S.diff_sales(state, [sale(3, 19.5)], today=TODAY)
    assert new == []                       # the same report read twice rings once


def test_a_brand_new_day_or_product_is_new():
    _, state = S.diff_sales(S.empty_state(), [sale(2, 13.0, date="2026-10-03")], today=TODAY)
    new, _ = S.diff_sales(state, [sale(2, 13.0, date="2026-10-03"), sale(1, 6.5), sale(1, 1.99, product="theme")], today=TODAY)
    assert sorted((s.product_id, s.quantity) for s in new) == [("pro", 1), ("theme", 1)]


def test_a_shrinking_row_never_rings_and_never_lowers_the_mark():
    # Microsoft can revise a day down (refunds); a later regrowth to the old mark is not new.
    _, state = S.diff_sales(S.empty_state(), [sale(4, 26.0)], today=TODAY)
    new, state = S.diff_sales(state, [sale(3, 19.5)], today=TODAY)
    assert new == [] and state["seen"][sale(4, 26.0).key]["qty"] == 4
    new, _ = S.diff_sales(state, [sale(4, 26.0)], today=TODAY)
    assert new == []


def test_old_rows_are_forgotten_after_the_keep_window():
    _, state = S.diff_sales(S.empty_state(), [sale(1, 6.5, date="2026-08-01"), sale(1, 6.5)], today=TODAY)
    assert list(state["seen"]) == [sale(1, 6.5).key]


def test_describe_one_and_many():
    title, body = S.describe([sale(1, 6.5)])
    assert title == "💰 New sale: Digital Rain Matrix Screensaver — Pro Upgrade" and "$6.50" in body
    title, body = S.describe([sale(2, 13.0), sale(1, 1.99, product="theme")])
    assert title == "💰 3 new sales · $14.99" and body.count("\n") == 1


def test_a_429_waits_out_its_hint_then_succeeds():
    calls, sleeps = [], []

    def request(method, url, token):
        calls.append(url)
        if len(calls) == 1:
            raise DevCenterError(429, '{"message": "Rate limit is exceeded. Try again in 3 seconds."}', url)
        return {"Value": []}

    S._paced(request, sleeps.append)("GET", "u", "t")
    assert len(calls) == 2 and 4 in sleeps   # the hint (3 s) + 1
    assert sleeps.count(S.CALL_SPACING_S) == 2


def test_a_non_429_error_is_not_retried():
    def request(method, url, token):
        raise DevCenterError(401, "unauthorized", url)

    with pytest.raises(DevCenterError):
        S._paced(request, lambda _s: None)("GET", "u", "t")


def test_check_end_to_end_with_a_fake_partner_center(tmp_path):
    state_file = tmp_path / "sales_state.json"
    report = {"qty": 1}

    def request(method, url, token):
        if "/applications" in url:
            return {"value": [{"id": APP["id"], "primaryName": APP["name"]}]}
        if "inappacquisitions" in url:
            return {"Value": [{"date": "2026-10-04", "acquisitionQuantity": report["qty"],
                               "purchasePriceUSDAmount": 6.5 * report["qty"], "addonProductId": "pro",
                               "inAppProductName": "Pro Upgrade"}]}
        return {"Value": []}

    first = S.check("tok", path=state_file, today=TODAY, request=request, sleep=lambda _s: None)
    assert first["baseline"] is True and first["new"] == [] and first["apps"] == 1
    report["qty"] = 2
    second = S.check("tok", path=state_file, today=TODAY, request=request, sleep=lambda _s: None)
    assert second["baseline"] is False and [s.quantity for s in second["new"]] == [1]
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["seeded"] is True and saved["last_check"] == second["checked"]


def test_a_locked_state_file_stops_the_check_instead_of_forgetting(tmp_path, monkeypatch):
    import navig_sdk.files as files

    def locked(*_a, **_k):
        raise files.JsonReadError("locked")

    monkeypatch.setattr(files, "load_json_for_update", locked)
    with pytest.raises(files.JsonReadError):
        S.check("tok", path=tmp_path / "s.json", today=TODAY, request=lambda *a: {}, sleep=lambda _s: None)
    assert not (tmp_path / "s.json").exists()


# ── The daily digest ──────────────────────────────────────────────────────────


def other(qty: int, usd: float, date: str) -> S.Sale:
    return S.Sale("9OTHER", "Pixel Clock", "9OTHER", "Pixel Clock", date, qty, usd)


def test_digest_reports_yesterday_and_the_last_seven_full_days():
    window = [
        sale(2, 13.0, "2026-10-03"),  # yesterday
        other(1, 2.99, "2026-10-03"),  # yesterday, a paid app
        sale(1, 6.5, "2026-09-27"),  # 7 days back: inside the week
        sale(5, 32.5, "2026-09-26"),  # 8 days back: outside
        sale(4, 26.0, "2026-10-04"),  # today: still filling up, left out
    ]
    title, body, data = S.digest(window, today=TODAY)
    assert title == "📊 Store sales Sat 3 Oct: 3 sales · $15.99"
    assert body.splitlines() == [
        "2× Digital Rain Matrix Screensaver — Pro Upgrade · $13.00",
        "1× Pixel Clock · $2.99",
        "Last 7 days: 4 sales · $22.49",
    ]
    assert data["date"] == "2026-10-03" and data["copies"] == 3 and data["total_usd"] == 15.99
    assert data["week_copies"] == 4 and data["week_usd"] == 22.49 and len(data["sales"]) == 2


def test_a_day_without_sales_still_gets_a_digest():
    title, body, data = S.digest([sale(1, 6.5, "2026-09-30")], today=TODAY)
    assert title == "📊 Store sales Sat 3 Oct: no sales"
    assert body == "Last 7 days: 1 sale · $6.50"
    assert data["copies"] == 0 and data["sales"] == []


def test_a_long_day_is_cut_to_a_few_lines():
    many = [S.Sale("A", f"App {i}", f"A{i}", f"App {i}", "2026-10-03", 1, 1.0 + i) for i in range(9)]
    _title, body, _data = S.digest(many, today=TODAY)
    lines = body.splitlines()
    assert len(lines) == S.DIGEST_MAX_LINES + 2 and lines[0].startswith("1× App 8")  # biggest first
    assert lines[-2] == "…and 3 more"


def test_digest_is_due_once_a_day_after_the_hour():
    morning = dt.datetime(2026, 10, 4, 8, 59)
    assert not S.digest_due(morning, 9, None)
    assert S.digest_due(morning.replace(hour=9), 9, None)
    assert S.digest_due(morning.replace(hour=9), 9, "2026-10-03")
    assert not S.digest_due(morning.replace(hour=23), 9, "2026-10-04")


def test_digest_state_round_trips(tmp_path):
    path = tmp_path / "digest_state.json"
    assert S.digest_sent_on(path) is None
    S.mark_digest_sent(dt.date(2026, 10, 4), path)
    assert S.digest_sent_on(path) == "2026-10-04"


def test_late_rows_are_listed_only_for_days_an_earlier_summary_reported():
    window = [sale(2, 13.0, "2026-10-01"), sale(1, 6.5, "2026-09-30")]
    known = {"2026-10-01": {"copies": 0, "usd": 0.0}}  # reported as empty; 09-30 never reported
    _title, body, data = S.digest(window, today=TODAY, known=known)
    assert data["late"] == [{"date": "2026-10-01", "copies": 2, "usd": 13.0}]
    assert "Arrived late: 2 sales · $13.00 for Thu 1 Oct" in body.splitlines()
    assert data["known"]["2026-10-01"] == {"copies": 2, "usd": 13.0}
    assert set(data["known"]) == {f"2026-09-{d}" for d in range(27, 31)} | {"2026-10-01", "2026-10-02", "2026-10-03"}


def test_a_day_that_shrank_or_stayed_is_not_late():
    known = {"2026-10-01": {"copies": 3, "usd": 19.5}}
    assert S.digest([sale(3, 19.5, "2026-10-01")], today=TODAY, known=known)[2]["late"] == []
    assert S.digest([sale(1, 6.5, "2026-10-01")], today=TODAY, known=known)[2]["late"] == []


def test_many_late_days_fold_into_one_line():
    known = {f"2026-09-{d}": {"copies": 0, "usd": 0} for d in range(27, 31)}
    window = [sale(1, 1.0, f"2026-09-{d}") for d in range(27, 31)]
    lines = S.digest(window, today=TODAY, known=known)[1].splitlines()
    assert sum(line.startswith("Arrived late") for line in lines) == S.LATE_MAX_LINES
    assert "…and 1 sale · $1.00 more on earlier days" in lines


def test_known_is_merged_and_pruned(tmp_path):
    path = tmp_path / "digest_state.json"
    S.mark_digest_sent(dt.date(2026, 10, 4), path, known={"2026-10-03": {"copies": 1, "usd": 1.0}},
                       previous={"known": {"2026-09-01": {"copies": 9, "usd": 9.0}, "2026-10-02": {"copies": 2, "usd": 2.0}}})
    state = S.load_digest_state(path)
    assert state["last_sent"] == "2026-10-04"
    assert set(state["known"]) == {"2026-10-02", "2026-10-03"}  # 09-01 is older than KNOWN_DAYS


# ── Sales history (the Store sales app) ───────────────────────────────────────


def test_history_never_shrinks_refreshes_names_and_forgets_old_rows():
    old = S.Sale(APP["id"], APP["name"], "pro", "Pro", "2025-01-01", 1, 6.5)  # beyond HISTORY_DAYS
    h = S.merge_history({}, [sale(3, 19.5, "2026-10-03"), old], today=TODAY)
    assert set(h["rows"]) == {sale(3, 19.5, "2026-10-03").key}
    h = S.merge_history(h, [sale(1, 6.5, "2026-10-03")], today=TODAY)  # a smaller re-read
    assert h["rows"][sale(1, 6.5, "2026-10-03").key]["quantity"] == 3
    renamed = S.Sale(APP["id"], APP["name"], "pro", "Pro Upgrade v2", "2026-10-03", 4, 26.0)
    h = S.merge_history(h, [renamed], today=TODAY)
    assert h["rows"][renamed.key]["product_name"] == "Pro Upgrade v2" and h["rows"][renamed.key]["quantity"] == 4


def test_history_series_has_every_day_and_ranks_apps_by_money():
    h = S.merge_history({}, [sale(2, 13.0, "2026-10-03"), other(1, 2.99, "2026-10-01"), other(5, 14.95, "2026-10-04")], today=TODAY)
    out = S.history_series(h, days=7, today=TODAY)
    assert [d["date"] for d in out["days"]] == [f"2026-09-{d}" for d in range(28, 31)] + [f"2026-10-0{d}" for d in range(1, 5)]
    assert out["days"][-1] == {"date": "2026-10-04", "copies": 5, "usd": 14.95}  # today, partial
    assert out["days"][0]["copies"] == 0  # an empty day is still a bar
    assert [a["app"] for a in out["apps"]] == ["Pixel Clock", APP["name"]]
    assert out["total"] == {"copies": 8, "usd": 30.94} and out["first_date"] == "2026-10-01"


def test_check_records_history_beside_its_state(tmp_path):
    state_file = tmp_path / "sales_state.json"

    def request(method, url, token):
        if "/applications" in url:
            return {"value": [{"id": APP["id"], "primaryName": APP["name"]}]}
        if "inappacquisitions" in url:
            return {"Value": [{"date": "2026-10-04", "acquisitionQuantity": 2, "purchasePriceUSDAmount": 13.0,
                               "addonProductId": "pro", "inAppProductName": "Pro Upgrade"}]}
        return {"Value": []}

    S.check("tok", path=state_file, today=TODAY, request=request, sleep=lambda _s: None)
    rows = S.load_history(tmp_path / "sales_history.json")["rows"]
    assert [r["quantity"] for r in rows.values()] == [2]


def test_backfill_fills_history_and_never_touches_the_ring_state(tmp_path):
    hist = tmp_path / "sales_history.json"
    seen_days = []

    def request(method, url, token):
        if "/applications" in url:
            return {"value": [{"id": APP["id"], "primaryName": APP["name"]}]}
        if "inappacquisitions" in url:
            seen_days.append(url)
            return {"Value": [{"date": "2026-08-01", "acquisitionQuantity": 1, "purchasePriceUSDAmount": 6.5,
                               "addonProductId": "pro", "inAppProductName": "Pro"}]}
        return {"Value": []}

    rows = S.backfill_history("tok", days=90, path=hist, today=TODAY, request=request, sleep=lambda _s: None)
    assert rows == 1 and "startDate=2026-07-06" in seen_days[0]
    saved = S.load_history(hist)
    assert saved["backfilled_days"] == 90 and len(saved["rows"]) == 1
    assert not (tmp_path / "sales_state.json").exists()


def test_a_summary_entry_is_kept_in_the_history_list(tmp_path):
    path = tmp_path / "digest_state.json"
    prev = {"history": [{"date": f"d{i}"} for i in range(S.DIGEST_HISTORY_MAX)]}
    S.mark_digest_sent(dt.date(2026, 10, 4), path, previous=prev, entry={"date": "2026-10-03", "title": "t"})
    hist = S.load_digest_state(path)["history"]
    assert len(hist) == S.DIGEST_HISTORY_MAX and hist[-1]["date"] == "2026-10-03" and hist[0]["date"] == "d1"
