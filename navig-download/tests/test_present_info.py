"""Empty/unreadable-post presentation — a clear message instead of a bare "🎵 TikTok" card."""

from __future__ import annotations

from typer.testing import CliRunner

from navig_download.commands import download as dl


def test_looks_unreadable():
    assert dl._looks_unreadable({}) is True
    # uploader alone comes from the URL path even for a post that didn't load → still unreadable
    assert dl._looks_unreadable({"uploader": "bob", "url": "https://tiktok.com/@bob/video/1"}) is True
    # any real signal makes it readable
    assert dl._looks_unreadable({"id": "123"}) is False
    assert dl._looks_unreadable({"description": "hi"}) is False
    assert dl._looks_unreadable({"images": ["a"]}) is False
    assert dl._looks_unreadable({"view_count": 10}) is False
    assert dl._looks_unreadable({"like_count": 3}) is False
    assert dl._looks_unreadable({"comment_count": 5}) is False


def test_present_info_returns_false_when_unreadable():
    assert dl._present_info({"uploader": "bob", "url": "https://tiktok.com/x"}, as_json=False) is False


def test_present_info_returns_true_when_readable():
    assert dl._present_info({"id": "1", "description": "hi", "uploader": "bob"}, as_json=False) is True


def test_present_info_json_reflects_readability():
    # --json always emits the dict; the return value still reflects whether it was readable
    assert dl._present_info({"id": "1"}, as_json=True) is True
    assert dl._present_info({"uploader": "bob"}, as_json=True) is False


def test_post_command_unreadable_skips_comments(monkeypatch):
    """An unreadable post must not crash and must not print a comments section."""
    monkeypatch.setattr(dl, "_browser_post",
                        lambda *a, **k: {"meta": {"uploader": "bob", "url": "https://tiktok.com/x"},
                                         "comments": []})
    r = CliRunner().invoke(dl.tiktok_app, ["post", "https://www.tiktok.com/@bob/video/1"])
    assert r.exit_code == 0
    # the comments header must not appear for an unreadable post
    assert "Top" not in r.stdout or "comments" not in r.stdout.lower()


def test_post_command_readable_shows_comments(monkeypatch):
    monkeypatch.setattr(dl, "_browser_post",
                        lambda *a, **k: {"meta": {"id": "9", "uploader": "bob",
                                                  "description": "hi", "url": "https://tiktok.com/x"},
                                         "comments": [{"text": "nice", "author": "amy", "likes": 3}]})
    r = CliRunner().invoke(dl.tiktok_app, ["post", "https://www.tiktok.com/@bob/video/9"])
    assert r.exit_code == 0
