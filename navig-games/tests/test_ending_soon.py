"""'Ending soon' urgency — a countdown + grab-before-gone ordering on the free feed.

The user can have a dozen giveaways waiting; nothing told them which expire first.
`days_until` turns each `ends_at` into a whole-day countdown, `runner.check` sorts
soonest-first and tags every row, and `navig games check` colour-grades the "Ends"
column and warns about anything ending within two days.
"""

from datetime import datetime, timezone

from typer.testing import CliRunner

from navig_games.commands.games import games_app
from navig_games.engine import runner
from navig_games.engine.models import FreeGame, days_until

runner_cli = CliRunner()
_NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)


# ── days_until ──────────────────────────────────────────────────────────────
def test_days_until_today_and_tomorrow():
    assert days_until("2026-07-17T18:00:00+00:00", now=_NOW) == 0
    assert days_until("2026-07-18T18:00:00+00:00", now=_NOW) == 1


def test_days_until_counts_whole_days():
    assert days_until("2026-07-20T12:00:00+00:00", now=_NOW) == 3


def test_days_until_past_clamps_to_zero():
    assert days_until("2026-07-16T00:00:00+00:00", now=_NOW) == 0


def test_days_until_none_for_open_ended_or_bad():
    assert days_until("", now=_NOW) is None
    assert days_until(None, now=_NOW) is None
    assert days_until("not-a-date", now=_NOW) is None


def test_days_until_accepts_space_and_naive_formats():
    # GamerPower ships "YYYY-MM-DD HH:MM:SS" (space, no tz) → treated as UTC.
    assert days_until("2026-07-19 23:59:00", now=_NOW) == 2


# ── runner.check: sort + tag ────────────────────────────────────────────────
def _game(key_slug, ends):
    return FreeGame(store="itch", title=key_slug, slug=key_slug, ends_at=ends,
                    url=f"https://x/{key_slug}")


def test_check_sorts_soonest_first_and_tags(monkeypatch):
    far = _game("far", "2026-07-25")     # 8 days
    soon = _game("soon", "2026-07-18")   # 1 day
    openended = _game("openended", "")   # None
    mid = _game("mid", "2026-07-19")     # 2 days
    games = [far, soon, openended, mid]
    monkeypatch.setattr(runner, "_source", lambda *args, **kw: (list(games), []))
    # Deterministic countdown regardless of the machine clock.
    counts = {"2026-07-25": 8, "2026-07-18": 1, "": None, "2026-07-19": 2}
    monkeypatch.setattr(runner, "days_until", lambda iso: counts.get(iso))
    monkeypatch.setattr(runner.Ledger, "get", lambda self, k: None)

    out = runner.check("giveaways")
    keys = [g["title"] for g in out["current"]]
    assert keys == ["soon", "mid", "far", "openended"]   # soonest first, open-ended last
    assert out["current"][0]["ends_in_days"] == 1
    assert out["current"][-1]["ends_in_days"] is None


# ── CLI check rendering ─────────────────────────────────────────────────────
def _fake_check(current):
    return {"store": "all", "country": "US", "locale": "en-US",
            "current": current, "upcoming": []}


def test_cli_check_grades_and_warns(monkeypatch):
    current = [
        {"store": "itch", "title": "Imminent", "key": "itch:1", "ends_in_days": 0,
         "claim_status": None, "original_price": "$5"},
        {"store": "gog", "title": "Later", "key": "gog:2", "ends_in_days": 20,
         "claim_status": None, "original_price": "$10"},
    ]
    monkeypatch.setattr(runner, "check", lambda **k: _fake_check(current))
    res = runner_cli.invoke(games_app, ["check"])
    assert res.exit_code == 0, res.output
    flat = " ".join(res.output.split())
    assert "today" in flat            # the 0-day row
    assert "in 20d" in flat           # the far row
    assert "⏰ 1 ending within 2 days" in flat


def test_cli_check_no_warning_when_nothing_imminent(monkeypatch):
    current = [{"store": "gog", "title": "Later", "key": "gog:2", "ends_in_days": 20,
               "claim_status": None, "original_price": "$10"}]
    monkeypatch.setattr(runner, "check", lambda **k: _fake_check(current))
    res = runner_cli.invoke(games_app, ["check"])
    assert res.exit_code == 0
    assert "⏰" not in res.output


def test_cli_check_settled_imminent_not_warned(monkeypatch):
    # Already grabbed/claimed → not counted as "grab it now".
    current = [{"store": "itch", "title": "Done", "key": "itch:1", "ends_in_days": 0,
                "claim_status": "grabbed", "original_price": "$5"}]
    monkeypatch.setattr(runner, "check", lambda **k: _fake_check(current))
    res = runner_cli.invoke(games_app, ["check"])
    assert res.exit_code == 0
    assert "⏰" not in res.output
