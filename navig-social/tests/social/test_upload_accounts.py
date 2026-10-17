"""A YouTube upload stays bound to the explicitly selected connected channel."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from navig_social.social import upload


def test_refresh_uses_the_selected_accounts_refresh_slot(monkeypatch):
    seen = []
    monkeypatch.setattr(upload, "refresh_provider", lambda provider, account=None: seen.append((provider, account)))
    monkeypatch.setattr(upload, "get_token", lambda provider, account=None: "second-token")

    assert upload.refresh_access_token("UC2") == "second-token"
    assert seen == [("youtube", "UC2")]


def test_refresh_failure_names_the_selected_channel(monkeypatch):
    def fail(provider, account=None):
        raise upload.SocialOAuthError("invalid_grant")

    monkeypatch.setattr(upload, "refresh_provider", fail)
    with pytest.raises(upload.UploadError, match="channel UC2"):
        upload.refresh_access_token("UC2")


def test_upload_refuses_a_token_for_a_different_channel(monkeypatch, tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(upload, "refresh_access_token", lambda account=None: "token")
    monkeypatch.setattr(
        upload, "channel_of", lambda token: {"id": "UC-WRONG", "title": "Wrong channel"}
    )

    with pytest.raises(upload.UploadError, match="credential/channel mismatch"):
        upload.upload(video, title="Clip", account="UC2")


def test_upload_fails_closed_when_google_cannot_identify_the_channel(monkeypatch, tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(upload, "refresh_access_token", lambda account=None: "token")
    monkeypatch.setattr(upload, "channel_of", lambda token: None)

    with pytest.raises(upload.UploadError, match="could not verify"):
        upload.upload(video, title="Clip", account="UC2")


def test_upload_rejects_a_legacy_default_without_a_recorded_channel_id(monkeypatch, tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(upload, "refresh_access_token", lambda account=None: "token")
    monkeypatch.setattr("navig_social.social.accounts.default_id", lambda provider: "")

    with pytest.raises(upload.UploadError, match="predates channel identity tracking"):
        upload.upload(video, title="Clip")


def test_cli_resolves_and_reports_the_selected_account_in_a_dry_run(monkeypatch, tmp_path):
    from navig_social.commands.social import social_app

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(
        "navig_social.social.accounts.resolve_account",
        lambda provider, selector: "UC2" if (provider, selector) == ("youtube", "Personal") else None,
    )

    result = CliRunner().invoke(
        social_app, ["upload", str(video), "--account", "Personal", "--dry-run", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert '"account": "UC2"' in result.output
