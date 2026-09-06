"""The live Epic session probe + its `status --check` / `doctor --live` surfaces.

`probe_live_login` is the authoritative "is the saved session still alive?" check
— the thing a vault-presence lookup can't tell you. These tests pin its four
outcomes (live / expired / can't-launch / bridge-error) and prove the CLI renders
each honestly (a saved-but-unverified session is never shown as "signed in").
"""

import pytest
from typer.testing import CliRunner

from navig_games.commands.games import games_app
from navig_games.engine.claim import epic

runner_cli = CliRunner()


# ── the engine primitive ────────────────────────────────────────────────────
class _FakeBridge:
    def __init__(self, page):
        self.page = page


def _patch_probe_internals(monkeypatch, *, port, logged_in, name="", bridge_raises=False):
    """Stub the three seams probe_live_login touches: profile launch, the session
    manager, and the egs-navigation read."""
    monkeypatch.setattr(epic, "open_epic_profile",
                        lambda profile="navig-epic": {"port": port} if port else {"error": "no chrome"})

    async def _nav(*a, **k):
        return {"ok": True}

    monkeypatch.setattr("navig.browser.cdp_actions.navigate", _nav)

    class _Mgr:
        async def get(self, port, tab):
            if bridge_raises:
                raise RuntimeError("cdp attach failed")
            return _FakeBridge(page=object())

    monkeypatch.setattr("navig.browser.session_manager.get_session_manager", lambda: _Mgr())

    async def _logged(_page):
        return logged_in, name

    monkeypatch.setattr(epic, "_is_logged_in", _logged)


@pytest.mark.asyncio
async def test_probe_reports_live_session_with_name(monkeypatch):
    _patch_probe_internals(monkeypatch, port=9999, logged_in=True, name="nevahudo")
    res = await epic.probe_live_login()
    assert res == {"ok": True, "signed_in": True, "name": "nevahudo", "error": None}


@pytest.mark.asyncio
async def test_probe_reports_expired_session(monkeypatch):
    # Reachable page, but Epic says logged out → ok=True (we DID look), signed_in=False.
    _patch_probe_internals(monkeypatch, port=9999, logged_in=False)
    res = await epic.probe_live_login()
    assert res["ok"] is True
    assert res["signed_in"] is False


@pytest.mark.asyncio
async def test_probe_could_not_launch_is_not_ok(monkeypatch):
    _patch_probe_internals(monkeypatch, port=None, logged_in=False)
    res = await epic.probe_live_login()
    assert res["ok"] is False
    assert "chrome" in (res["error"] or "").lower()


@pytest.mark.asyncio
async def test_probe_bridge_error_is_not_ok(monkeypatch):
    _patch_probe_internals(monkeypatch, port=9999, logged_in=True, bridge_raises=True)
    res = await epic.probe_live_login()
    assert res["ok"] is False
    assert res["error"]


# ── the CLI surfaces ────────────────────────────────────────────────────────
def _stub_presence(monkeypatch, present, name=None):
    monkeypatch.setattr(epic, "epic_session_present", lambda: (present, name))


def _stub_probe(monkeypatch, result):
    async def _p(profile="navig-epic"):
        return result

    monkeypatch.setattr(epic, "probe_live_login", _p)


def test_status_default_shows_saved_not_signed_in(monkeypatch):
    _stub_presence(monkeypatch, True, "nevahudo")
    res = runner_cli.invoke(games_app, ["status"])
    assert res.exit_code == 0, res.output
    assert "session saved" in res.output
    assert "nevahudo" in res.output
    # Must NOT overclaim liveness without --check.
    assert "live" not in res.output.lower()


def test_status_not_signed_in(monkeypatch):
    _stub_presence(monkeypatch, False, None)
    res = runner_cli.invoke(games_app, ["status"])
    assert res.exit_code == 0
    assert "not signed in" in res.output


def test_status_check_reports_live(monkeypatch):
    _stub_presence(monkeypatch, True, "nevahudo")
    _stub_probe(monkeypatch, {"ok": True, "signed_in": True, "name": "nevahudo", "error": None})
    res = runner_cli.invoke(games_app, ["status", "--check"])
    assert res.exit_code == 0, res.output
    assert "live" in res.output.lower()


def test_status_check_reports_expired(monkeypatch):
    _stub_presence(monkeypatch, True, "nevahudo")
    _stub_probe(monkeypatch, {"ok": True, "signed_in": False, "name": "", "error": None})
    res = runner_cli.invoke(games_app, ["status", "--check"])
    assert res.exit_code == 0
    assert "expired" in res.output.lower()


def test_doctor_default_does_not_claim_live(monkeypatch):
    _stub_presence(monkeypatch, True, "nevahudo")
    res = runner_cli.invoke(games_app, ["doctor"])
    assert res.exit_code == 0, res.output
    assert "Epic session" in res.output
    assert "--live" in res.output  # tells the user how to actually verify


def test_doctor_live_flags_expired_session(monkeypatch):
    _stub_presence(monkeypatch, True, "nevahudo")
    _stub_probe(monkeypatch, {"ok": True, "signed_in": False, "name": "", "error": None})
    res = runner_cli.invoke(games_app, ["doctor", "--live"])
    # An expired session is a FAILED check, so the doctor exits non-zero — that is what
    # separates it from the could-not-verify case below, which stays a warning at exit 0.
    # This assertion read `== 0` only because every doctor outcome used to exit 0, so it
    # could not tell the two apart.
    assert res.exit_code == 1
    assert "EXPIRED" in res.output


def test_doctor_live_could_not_verify_is_a_warning(monkeypatch):
    _stub_presence(monkeypatch, True, "nevahudo")
    _stub_probe(monkeypatch, {"ok": False, "signed_in": False, "name": "", "error": "no chrome"})
    res = runner_cli.invoke(games_app, ["doctor", "--live"])
    assert res.exit_code == 0
    assert "couldn't verify" in res.output.lower()
