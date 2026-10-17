"""`social status` must not call a YouTube connection "ready" when it cannot renew.

Found 2026-10-10: the vault held a YouTube access token but no refresh token,
status printed "connected · ready", and every scheduled upload would have
failed an hour after `connect`.
"""
from __future__ import annotations

from navig_social.social import oauth


def _vault(monkeypatch, secrets: dict[str, str]) -> None:
    monkeypatch.setattr(oauth, "read_secret", lambda path: secrets.get(path))


def _row(name: str) -> dict:
    return next(r for r in oauth.provider_status() if r["provider"] == name)


def test_youtube_without_refresh_token_is_flagged(monkeypatch):
    _vault(monkeypatch, {"youtube": "ya29.access", "youtube/client_id": "c",
                         "youtube/client_secret": "s"})
    row = _row("youtube")
    assert row["token"] is True
    assert row["refresh"] is False


def test_youtube_with_refresh_token_is_ready(monkeypatch):
    _vault(monkeypatch, {"youtube": "ya29.access", "youtube/refresh_token": "1//r",
                         "youtube/client_id": "c", "youtube/client_secret": "s"})
    assert _row("youtube")["refresh"] is True


def test_providers_without_refresh_tokens_are_not_judged(monkeypatch):
    _vault(monkeypatch, {"devto": "k"})
    assert _row("devto")["refresh"] is None


def test_status_table_says_reconnect(monkeypatch):
    from typer.testing import CliRunner
    from navig_social.commands.social import social_app as app

    _vault(monkeypatch, {"youtube": "ya29.access", "youtube/client_id": "c",
                         "youtube/client_secret": "s"})
    out = CliRunner().invoke(app, ["status"], env={"COLUMNS": "200"}).output
    line = next(ln for ln in out.splitlines() if "YouTube" in ln)
    assert "no refresh token" in line
    assert "ready" not in line
