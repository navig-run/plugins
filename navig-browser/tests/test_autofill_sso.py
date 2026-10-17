"""auto_login with an SSO-linked credential must never reach the form-fill path.

An SSO record (`navig vault login capture`) has no password. When its session is
dead the ONLY correct outcome is a loud `session_expired` telling the operator
to re-capture — submitting an empty password would be a silent no-op at best and
an account-lockout risk at worst. The second test pins the other direction: a
legacy password login still reaches form-fill, so the guard cannot widen and
swallow normal logins.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from navig_browser.autofill import auto_login


class _NoPage:
    """Any attribute access proves the form-fill path ran."""

    def __getattr__(self, name):
        raise AssertionError(f"form-fill touched the page: {name!r}")


class _Controller:
    def __init__(self, url="https://example.com/app", page=None):
        self._url = url
        self._page = page if page is not None else _NoPage()

    async def get_url(self):
        return self._url


class _FillPage:
    """A page that answers the fill probe like 'no password field here'."""

    async def evaluate(self, _js, _creds):
        return {"hasPassword": False, "filled": False}


def _patch_vault(monkeypatch, *, login, session=None):
    """Bind resolve_login/get_session for this call — both are imported
    inside auto_login, so patching the source modules is the seam."""
    import navig_vault.logins as logins
    import navig_vault.sessions as sessions

    monkeypatch.setattr(logins, "resolve_login", lambda *a, **k: (login, "ok"))
    monkeypatch.setattr(logins, "find_logins_for_domain", lambda *a, **k: [])
    monkeypatch.setattr(sessions, "get_session", lambda *a, **k: session)


def test_sso_login_with_dead_session_fails_loudly_without_touching_the_page(monkeypatch):
    from navig_vault.logins import WebLogin

    sso = WebLogin(
        domain="example.com",
        username="you@example.com",
        password="",  # there is none — the whole point
        auth_method="sso",
        idp="google",
        idp_subject="you@gmail.com",
    )
    _patch_vault(monkeypatch, login=sso, session=None)

    res = asyncio.run(auto_login(_Controller(), domain="example.com"))

    assert res["status"] == "session_expired"
    assert res["idp"] == "google"
    assert "navig vault login capture example.com" in res["detail"]
    # _NoPage raises on ANY access — reaching here means the fill JS ran.


def test_legacy_password_login_still_reaches_form_fill(monkeypatch):
    legacy = SimpleNamespace(
        domain="example.com",
        username="you@example.com",
        password="s3cret",
        totp_secret=None,
        # no auth_method attribute at all — getattr must default to password
    )
    _patch_vault(monkeypatch, login=legacy, session=None)

    res = asyncio.run(auto_login(_Controller(page=_FillPage()), domain="example.com"))

    # Reached the fill probe (hasPassword False → no_login_form), i.e. the SSO
    # guard did NOT fire for a legacy credential.
    assert res["status"] == "no_login_form"


def test_session_expired_points_at_the_linked_api_credential(monkeypatch):
    from navig_vault.logins import WebLogin

    sso = WebLogin(
        domain="example.com",
        username="you",
        password="",
        auth_method="sso",
        idp="google",
        api_ref="notion",
    )
    _patch_vault(monkeypatch, login=sso, session=None)

    res = asyncio.run(auto_login(_Controller(), domain="example.com"))

    assert res["status"] == "session_expired"
    assert res["api_ref"] == "notion"
    assert "agent-side actions" in res["detail"]
    assert "navig vault login capture example.com" in res["detail"]


def test_session_expired_without_an_api_credential_stays_quiet(monkeypatch):
    from navig_vault.logins import WebLogin

    sso = WebLogin(domain="example.com", username="you", password="", auth_method="sso")
    _patch_vault(monkeypatch, login=sso, session=None)

    res = asyncio.run(auto_login(_Controller(), domain="example.com"))

    assert res["status"] == "session_expired"
    assert res["api_ref"] is None
    assert "agent-side actions" not in res["detail"]
