"""`grabbed` — the terminal state for every game we can't claim headlessly.

Without it a giveaway (itch / IndieGala / Stove / GOG / Steam free-to-keep) can
never settle: it resurfaces in the Free tab forever and sits in History as an
amber "needs you". These tests pin the state machine and its two safety rules —
un-marking never rewrites real claim history, and marking never blocks a
deliberate per-game claim.
"""

import asyncio

import pytest

from navig_games.engine import runner
from navig_games.engine.claim.base import (
    STATUS_CLAIMED,
    STATUS_GRABBED,
    STATUS_MANUAL,
    STATUS_OWNED,
)
from navig_games.engine.ledger import Ledger
from navig_games.engine.models import FreeGame


def _giveaway(key: str = "3697") -> FreeGame:
    return FreeGame(store="itch", title="NIGHTBELL", offer_id=key,
                    url="https://www.gamerpower.com/open/nightbell",
                    instructions="1. Click the button. 2. Log in.")


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """An isolated ledger, read FRESH on every access.

    ``mark_grabbed`` builds its own Ledger, so a snapshot held by the test would go
    stale the moment the engine writes — the same trap the engine itself hits across
    the claim subprocess (hence ``Ledger._reload``).
    """
    path = tmp_path / "claimed.json"
    monkeypatch.setattr(runner, "Ledger", lambda: Ledger(path=path))
    return lambda: Ledger(path=path)


@pytest.fixture(autouse=True)
def _one_giveaway(monkeypatch):
    """The only game that's free right now. No network in any of these tests."""
    monkeypatch.setattr("navig_games.engine.sources.epic.fetch_free_games",
                        lambda country=None, locale=None: {"current": [], "upcoming": []})
    monkeypatch.setattr("navig_games.engine.sources.steam.free_to_keep",
                        lambda cc="us", **kw: [])
    monkeypatch.setattr("navig_games.engine.sources.giveaways.fetch",
                        lambda **kw: [_giveaway()])


# ── the happy path ───────────────────────────────────────────────────────────


def test_grab_settles_a_giveaway(ledger):
    game = _giveaway()
    r = runner.mark_grabbed(game.key)

    assert r["changed"] is True
    assert r["status"] == STATUS_GRABBED
    assert r["title"] == "NIGHTBELL"
    rec = ledger().get(game.key)
    assert rec["status"] == STATUS_GRABBED
    assert rec["store"] == "itch"          # full metadata, not just a key
    assert ledger().is_settled(game.key)     # → it stops resurfacing
    assert ledger().unseen([game]) == []


def test_undo_puts_it_back_on_the_list(ledger):
    game = _giveaway()
    runner.mark_grabbed(game.key)

    r = runner.mark_grabbed(game.key, grabbed=False)
    assert r["changed"] is True
    assert ledger().get(game.key) is None
    assert ledger().unseen([game]) == [game]


def test_grab_is_idempotent(ledger):
    game = _giveaway()
    runner.mark_grabbed(game.key)
    runner.mark_grabbed(game.key)  # again — must not corrupt or duplicate
    assert ledger().get(game.key)["status"] == STATUS_GRABBED
    assert len(ledger().all()) == 1


# ── the safety rules ─────────────────────────────────────────────────────────


def test_undo_refuses_to_rewrite_real_claim_history(ledger):
    """A game WE claimed is history, not a user note — undo must not erase it."""
    game = _giveaway()
    ledger().record(game, STATUS_CLAIMED)

    r = runner.mark_grabbed(game.key, grabbed=False)
    assert r["changed"] is False
    assert "error" in r
    assert ledger().get(game.key)["status"] == STATUS_CLAIMED  # untouched


def test_grabbing_an_owned_game_never_downgrades_it(ledger):
    game = _giveaway()
    ledger().record(game, STATUS_OWNED, note="already in your library")

    r = runner.mark_grabbed(game.key)
    assert r["changed"] is False
    assert r["message"] == "already in your library"
    assert ledger().get(game.key)["status"] == STATUS_OWNED  # not overwritten


def test_grab_overrides_a_needs_manual_record(ledger):
    """The daily deals run records new giveaways as needs_manual — grabbing resolves it."""
    game = _giveaway()
    ledger().record(game, STATUS_MANUAL, note="grab it yourself")

    assert runner.mark_grabbed(game.key)["changed"] is True
    assert ledger().get(game.key)["status"] == STATUS_GRABBED  # amber → green


def test_grab_refuses_a_game_that_isnt_free(ledger):
    r = runner.mark_grabbed("itch:nope")
    assert r["changed"] is False
    assert "isn't free" in r["error"]
    assert ledger().all() == []  # nothing invented


def test_undo_of_an_unmarked_game_is_a_no_op(ledger):
    r = runner.mark_grabbed("itch:nope", grabbed=False)
    assert r["changed"] is False
    assert "error" not in r


# ── the claim path still wins ────────────────────────────────────────────────


def test_grabbed_is_skipped_by_the_scheduled_claim_but_not_by_a_forced_one(
    ledger, monkeypatch
):
    """Marking doubles as "skip this" — yet `claim --game <key>` must still override."""
    epic = FreeGame(store="epic", title="Tattoo Tycoon", offer_id="tt", url="http://e/tt")
    monkeypatch.setattr("navig_games.engine.sources.epic.fetch_free_games",
                        lambda country=None, locale=None: {"current": [epic], "upcoming": []})
    monkeypatch.setattr("navig_games.engine.sources.giveaways.fetch", lambda **kw: [])

    runner.mark_grabbed(epic.key)
    assert ledger().unseen([epic]) == []  # the scheduled run skips it

    seen: list[str] = []

    async def _fake_claims(games, **kw):
        seen.extend(g.key for g in games)
        return [], None

    monkeypatch.setattr("navig_games.engine.claim.epic.run_epic_claims", _fake_claims)
    monkeypatch.setattr(runner.last_run, "record", lambda summary: None)

    asyncio.run(runner.run_claim(only=epic.key, do_notify=False))
    assert seen == [epic.key]  # …but the explicit per-game claim still ran it
