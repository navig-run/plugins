"""How the mailroom chooses its Gmail: navig's OAuth connector when linked, else the IMAP app password."""

from __future__ import annotations

import asyncio

import pytest
from typer.testing import CliRunner

from navig_email import account, gmail, imap_account
from navig_email.commands.email import email_app
from navig_email.imap import GmailImap

runner = CliRunner()


@pytest.fixture
def no_oauth(monkeypatch):
    monkeypatch.setattr(gmail, "oauth_connected", lambda account=None: False)


def test_imap_backend_is_used_when_oauth_is_not_linked(no_oauth, monkeypatch):
    monkeypatch.setattr(imap_account, "load", lambda: ("me@gmail.com", "app-pass"))
    c = asyncio.run(gmail._connector())
    assert isinstance(c, GmailImap) and c.user == "me@gmail.com"
    assert gmail.is_connected() and account.connected_email() == "me@gmail.com"
    assert "me@gmail.com" in account.linked_accounts()
    assert asyncio.run(gmail._connector("someone.else@gmail.com")) is None  # a different account is not it


def test_oauth_wins_when_linked(monkeypatch):
    sentinel = object()

    async def _oauth(account=None):
        return sentinel

    monkeypatch.setattr(gmail, "oauth_connected", lambda account=None: True)
    monkeypatch.setattr(gmail, "_oauth_connector", _oauth)
    monkeypatch.setattr(imap_account, "load", lambda: ("me@gmail.com", "app-pass"))
    assert asyncio.run(gmail._connector()) is sentinel


def test_not_connected_names_the_app_password_route(no_oauth, monkeypatch):
    monkeypatch.setattr(imap_account, "load", lambda: None)
    with pytest.raises(account.NotConnected, match="imap connect"):
        asyncio.run(account.connector())


def test_imap_connect_saves_nothing_when_gmail_refuses(monkeypatch):
    saved = []
    monkeypatch.setattr(imap_account, "save", lambda u, p: saved.append((u, p)))

    async def _refuse(self):
        from navig_email.errors import ConnectorAuthError

        raise ConnectorAuthError("gmail-imap", "Gmail refused the sign-in. Use an APP PASSWORD")

    monkeypatch.setattr(GmailImap, "get_profile", _refuse)
    r = runner.invoke(email_app, ["imap", "connect", "me@gmail.com", "--password", "wrong"])
    assert r.exit_code == 1 and "refused" in r.output and saved == []


def test_imap_connect_saves_after_a_working_sign_in(monkeypatch):
    saved = []
    monkeypatch.setattr(imap_account, "save", lambda u, p: saved.append((u, p)))

    async def _ok(self):
        return {"emailAddress": self.user, "messagesTotal": 42, "historyId": "imap:1:2"}

    monkeypatch.setattr(GmailImap, "get_profile", _ok)
    r = runner.invoke(email_app, ["imap", "connect", "me@gmail.com", "--password", "abcd efgh ijkl mnop"])
    assert r.exit_code == 0, r.output
    assert saved == [("me@gmail.com", "abcd efgh ijkl mnop")] and "42 messages" in r.output


def test_imap_status_never_prints_the_password(monkeypatch):
    monkeypatch.setattr(imap_account, "load", lambda: ("me@gmail.com", "s3cret-app-pass"))
    monkeypatch.setattr(imap_account, "source", lambda: "vault")
    r = runner.invoke(email_app, ["imap", "status"])
    assert r.exit_code == 0 and "me@gmail.com" in r.output and "s3cret" not in r.output


def test_env_credentials_win_and_the_space_is_removed(monkeypatch):
    monkeypatch.setenv(imap_account.ENV_USER, "ci@gmail.com")
    monkeypatch.setenv(imap_account.ENV_PASSWORD, "abcd efgh")
    assert imap_account.load() == ("ci@gmail.com", "abcdefgh")  # display spaces dropped
    assert imap_account.source() == "env"
