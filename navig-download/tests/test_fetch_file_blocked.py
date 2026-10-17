"""The DOWNLOAD path was the one route that never reported a bot-wall.

`info` and `info_with_comments` both run `_raise_for_extractor_error` and are both tested
for it. `fetch_file` — the route every *button* takes — did not, so a 403/429/
captcha surfaced as a bare yt-dlp error. Three handlers in
`navig.telegram.tiktok_actions` were written to catch `TikTokBlocked` on exactly
this path (Transcript, Audio, and now Download) and could never fire: the user
hit TikTok's most common failure and was told "couldn't download that video",
with no hint that it is transient or that a proxy/cookies would fix it.
"""

from __future__ import annotations

import sys
import types

import pytest

from navig_download.tiktok import engine


def _install_fake_ytdlp(monkeypatch, exc):
    """Make `fetch_file` reach yt-dlp and have extract_info raise *exc*."""

    class _FakeYDL:
        def __init__(self, opts):
            self._opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            raise exc

        def prepare_filename(self, data):  # pragma: no cover — never reached
            return "/tmp/1.mp4"

    fake = types.ModuleType("yt_dlp")
    fake.YoutubeDL = _FakeYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)
    monkeypatch.setattr(engine, "ytdlp_available", lambda: True, raising=False)
    monkeypatch.setattr(
        "navig_download.tiktok.ytdlp_available", lambda: True, raising=False
    )


@pytest.mark.parametrize(
    "message",
    [
        "HTTP Error 403: Forbidden",
        "HTTP Error 429: Too Many Requests",
        "captcha required",
        "Unable to extract webpage video data",
    ],
)
def test_fetch_file_escalates_a_bot_wall(monkeypatch, tmp_path, message):
    _install_fake_ytdlp(monkeypatch, RuntimeError(message))
    with pytest.raises(engine.TikTokBlocked):
        engine.fetch_file("https://www.tiktok.com/@x/video/1", dest_dir=str(tmp_path))


def test_fetch_file_reraises_a_benign_error_unchanged(monkeypatch, tmp_path):
    """Not every failure is a wall — a genuine bug must stay a genuine bug."""
    _install_fake_ytdlp(monkeypatch, ValueError("weird but not a wall"))
    with pytest.raises(ValueError):
        engine.fetch_file("https://www.tiktok.com/@x/video/1", dest_dir=str(tmp_path))


def test_fetch_file_does_not_disguise_unavailable(monkeypatch, tmp_path):
    """`TikTokUnavailable` means "no downloader installed" — a different fix."""
    _install_fake_ytdlp(monkeypatch, engine.TikTokUnavailable("no yt-dlp"))
    with pytest.raises(engine.TikTokUnavailable):
        engine.fetch_file("https://www.tiktok.com/@x/video/1", dest_dir=str(tmp_path))


async def test_fetch_file_async_carries_the_classification(monkeypatch, tmp_path):
    """The bot calls the async wrapper; the thread hop must not lose the type."""
    _install_fake_ytdlp(monkeypatch, RuntimeError("HTTP Error 403: Forbidden"))
    with pytest.raises(engine.TikTokBlocked):
        await engine.fetch_file_async(
            "https://www.tiktok.com/@x/video/1", dest_dir=str(tmp_path)
        )
