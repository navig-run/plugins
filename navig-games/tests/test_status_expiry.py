"""Cheap (no-browser) Epic sign-in expiry signal on the status surfaces.

A vaulted session can be *present* yet *dead*. Rather than probe a browser on
every status poll, we derive "the last auto-claim couldn't sign in" from the
`last_run` rollup the scheduled claim already writes — surfaced in the deck
status payload (`epic_session_expired`) and as a CLI "Last claim" row.
"""

import asyncio

from typer.testing import CliRunner

from navig_games.commands.games import games_app
from navig_games.engine import last_run
from navig_games.engine.claim import epic

runner_cli = CliRunner()


def _run(coro):
    return asyncio.run(coro)


# ── shared helper ───────────────────────────────────────────────────────────
def test_login_needs_signin_true_on_needs_manual(monkeypatch):
    monkeypatch.setattr(last_run, "read", lambda: {"login": "needs_manual", "claimed": 0})
    assert last_run.login_needs_signin() is True


def test_login_needs_signin_false_when_signed_in(monkeypatch):
    monkeypatch.setattr(last_run, "read", lambda: {"login": "session_restored", "claimed": 1})
    assert last_run.login_needs_signin() is False


def test_login_needs_signin_false_when_no_run(monkeypatch):
    monkeypatch.setattr(last_run, "read", lambda: None)
    assert last_run.login_needs_signin() is False


# ── epic_session_expired() resolution logic ─────────────────────────────────
def _expiry(monkeypatch, run, captured_at):
    monkeypatch.setattr(last_run, "read", lambda: run)
    monkeypatch.setattr(epic, "epic_last_captured_at", lambda: captured_at)
    return epic.epic_session_expired()


def test_expired_none_when_no_run(monkeypatch):
    assert _expiry(monkeypatch, None, None) is False


def test_expired_false_when_signed_in(monkeypatch):
    assert _expiry(monkeypatch, {"login": "session_restored"}, None) is False


def test_expired_true_when_no_session_captured(monkeypatch):
    run = {"login": "needs_manual", "finished_at": "2026-07-17T10:00:00+00:00"}
    assert _expiry(monkeypatch, run, None) is True


def test_expired_true_when_session_older_than_failed_run(monkeypatch):
    run = {"login": "needs_manual", "finished_at": "2026-07-17T10:00:00+00:00"}
    assert _expiry(monkeypatch, run, "2026-07-17T09:00:00+00:00") is True


def test_expired_false_when_relogged_after_run(monkeypatch):
    run = {"login": "needs_manual", "finished_at": "2026-07-17T10:00:00+00:00"}
    assert _expiry(monkeypatch, run, "2026-07-17T11:00:00+00:00") is False


def test_expired_true_when_timestamps_unparseable(monkeypatch):
    # Fail toward surfacing: if we can't confirm a newer sign-in, stay expired.
    run = {"login": "needs_manual", "finished_at": "garbage"}
    assert _expiry(monkeypatch, run, "also-not-a-date") is True


# ── deck status payload ─────────────────────────────────────────────────────
def _status(monkeypatch, last):
    from navig_games.engine import library

    class _G:
        store = "steam"

        def to_dict(self):
            return {}

    monkeypatch.setattr(library, "scan", lambda: [_G()])
    monkeypatch.setattr(last_run, "read", lambda: last)
    # No re-login since the failed run → a needs_manual run stays "expired"
    # deterministically (the real vault would otherwise leak in).
    monkeypatch.setattr(epic, "epic_last_captured_at", lambda: None)
    from navig_games.deck_routes import games as gr

    return gr._status_payload()


def test_status_payload_flags_expired(monkeypatch):
    d = _status(monkeypatch, {"login": "needs_manual", "finished_at": "2026-07-16T00:00:00+00:00"})
    assert d["epic_session_expired"] is True


def test_status_payload_not_expired_when_signed_in(monkeypatch):
    d = _status(monkeypatch, {"login": "session_restored"})
    assert d["epic_session_expired"] is False


def test_status_payload_not_expired_when_no_run(monkeypatch):
    d = _status(monkeypatch, None)
    assert d["epic_session_expired"] is False


# ── CLI 'Last claim' row ────────────────────────────────────────────────────
def _stub_login_row(monkeypatch):
    # Keep the Epic-login row deterministic (no real vault / browser).
    monkeypatch.setattr(epic, "epic_session_present", lambda: (True, "tester"))


def test_cli_status_flags_expired_signin(monkeypatch):
    _stub_login_row(monkeypatch)
    monkeypatch.setattr(epic, "epic_last_captured_at", lambda: None)  # no re-login
    monkeypatch.setattr(last_run, "read",
                        lambda: {"login": "needs_manual", "claimed": 0, "needs_manual": 2,
                                 "finished_at": "2026-07-16T00:00:00+00:00"})
    res = runner_cli.invoke(games_app, ["status"])
    assert res.exit_code == 0, res.output
    flat = " ".join(res.output.split())   # collapse Rich's line-wrapping
    assert "Last claim" in flat
    assert "couldn't sign in" in flat
    assert "run navig games login epic" in flat   # the actionable nudge


def test_cli_status_expiry_resolved_after_relogin(monkeypatch):
    """Re-authorized since the failed run → drop the stale 'run login epic' nudge."""
    _stub_login_row(monkeypatch)
    monkeypatch.setattr(last_run, "read",
                        lambda: {"login": "needs_manual", "claimed": 0, "needs_manual": 2,
                                 "finished_at": "2026-07-16T00:00:00+00:00"})
    # A session captured AFTER the failed run.
    monkeypatch.setattr(epic, "epic_last_captured_at", lambda: "2026-07-16T12:00:00+00:00")
    res = runner_cli.invoke(games_app, ["status"])
    assert res.exit_code == 0, res.output
    flat = " ".join(res.output.split())
    assert "re-authorized" in flat.lower()
    assert "login epic" not in flat   # the stale nudge is gone


def test_cli_status_shows_claimed(monkeypatch):
    _stub_login_row(monkeypatch)
    monkeypatch.setattr(last_run, "read",
                        lambda: {"login": "session_restored", "claimed": 1, "needs_manual": 0,
                                 "finished_at": "2026-07-16T00:00:00+00:00"})
    res = runner_cli.invoke(games_app, ["status"])
    assert res.exit_code == 0
    assert "claimed" in res.output.lower()


def test_cli_status_no_last_claim_row_when_never_run(monkeypatch):
    _stub_login_row(monkeypatch)
    monkeypatch.setattr(last_run, "read", lambda: None)
    res = runner_cli.invoke(games_app, ["status"])
    assert res.exit_code == 0
    assert "Last claim" not in res.output


# ── age helper ──────────────────────────────────────────────────────────────
def test_ago_handles_bad_input():
    from navig_games.commands.games import _ago

    assert _ago(None) == ""
    assert _ago("not-a-date") == ""
    assert _ago("2020-01-01T00:00:00+00:00").endswith("ago")
