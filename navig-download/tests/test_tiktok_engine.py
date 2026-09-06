"""Unit tests for the TikTok engine's block-vs-empty honesty (Stage 1)."""
from __future__ import annotations

import pytest

from navig_download.tiktok import engine


class _FakeYDL:
    """Stand-in for yt_dlp.YoutubeDL used as a context manager."""

    def __init__(self, data=None, exc=None):
        self._data = data
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        if self._exc is not None:
            raise self._exc
        return self._data


def _patch_ydl(monkeypatch, *, data=None, exc=None):
    monkeypatch.setattr(engine, "_ydl", lambda **kw: _FakeYDL(data=data, exc=exc))


def test_raise_for_extractor_error_converts_bot_wall():
    with pytest.raises(engine.TikTokBlocked):
        engine._raise_for_extractor_error(RuntimeError("HTTP Error 403: Forbidden"))


def test_raise_for_extractor_error_passes_benign_through():
    # Benign error is NOT converted (returns None; caller re-raises the original).
    assert engine._raise_for_extractor_error(RuntimeError("some parsing quirk")) is None


def test_info_escalates_block(monkeypatch):
    _patch_ydl(monkeypatch, exc=RuntimeError("HTTP Error 429: Too Many Requests"))
    with pytest.raises(engine.TikTokBlocked):
        engine.info("https://www.tiktok.com/@x/video/1")


def test_info_reraises_non_block(monkeypatch):
    _patch_ydl(monkeypatch, exc=ValueError("weird but not a wall"))
    with pytest.raises(ValueError):
        engine.info("https://www.tiktok.com/@x/video/1")


def test_info_unavailable_not_swallowed(monkeypatch):
    def _boom(**kw):
        raise engine.TikTokUnavailable("no yt-dlp")

    monkeypatch.setattr(engine, "_ydl", _boom)
    with pytest.raises(engine.TikTokUnavailable):
        engine.info("https://www.tiktok.com/@x/video/1")


def test_comments_blocked_flag_true_when_gated(monkeypatch):
    """comment_count>0 but none returned → comments_blocked=True (the gated-signed case)."""
    _patch_ydl(monkeypatch, data={
        "id": "1", "title": "t", "description": "d", "comment_count": 666, "comments": [],
    })
    out = engine.info_with_comments("https://www.tiktok.com/@x/video/1")
    assert out["comments_blocked"] is True
    assert out["comments"] == []


def test_comments_blocked_flag_false_when_genuinely_empty(monkeypatch):
    """comment_count==0 and none returned → genuinely empty, NOT blocked."""
    _patch_ydl(monkeypatch, data={
        "id": "1", "title": "t", "description": "d", "comment_count": 0, "comments": [],
    })
    out = engine.info_with_comments("https://www.tiktok.com/@x/video/1")
    assert out["comments_blocked"] is False


def test_comments_blocked_flag_false_when_present(monkeypatch):
    _patch_ydl(monkeypatch, data={
        "id": "1", "title": "t", "description": "d", "comment_count": 2,
        "comments": [{"text": "hi", "like_count": 3, "author": "a"}],
    })
    out = engine.info_with_comments("https://www.tiktok.com/@x/video/1")
    assert out["comments_blocked"] is False
    assert out["comments"][0]["text"] == "hi"


def test_info_with_comments_escalates_block(monkeypatch):
    _patch_ydl(monkeypatch, exc=RuntimeError("captcha required"))
    with pytest.raises(engine.TikTokBlocked):
        engine.info_with_comments("https://www.tiktok.com/@x/video/1")


# ── briefing: language + the sections it produces ─────────────────────────────


def test_language_directive_mirrors_the_source_on_auto():
    """A Russian clip summarised in English forces the operator to translate back
    what the comments already said."""
    assert "SAME language" in engine._lang_directive(None)
    assert "SAME language" in engine._lang_directive("auto")
    assert "SAME language" in engine._lang_directive("  AUTO  ")


def test_language_directive_honours_an_explicit_language():
    assert "Russian" in engine._lang_directive("Russian")
    assert "SAME language" not in engine._lang_directive("Russian")


def test_the_briefing_asks_for_substance_not_a_watch_verdict():
    """'Worth watching?' answered a question the reader settled by opening it."""
    assert "Worth watching" not in engine._BRIEF_SYSTEM
    assert "Worth knowing" in engine._BRIEF_SYSTEM


def test_brief_payload_carries_the_metadata_the_header_shows():
    payload = engine._brief_payload({
        "uploader": "tilga_photo", "uploader_id": "tilga_photo",
        "country": "Latvia", "upload_date": "20260804", "duration": 17,
        "track": "original sound", "view_count": 1234,
        "description": "hello", "comments": [],
    })
    for expected in ("tilga_photo", "Latvia", "20260804", "17s",
                     "original sound", "views=1,234"):
        assert expected in payload, f"missing {expected!r} in:\n{payload}"


def test_audio_only_selects_an_audio_format(monkeypatch):
    """The audio action and the transcript path must not pull the whole video."""
    captured = {}

    class _FakeYDL:
        def __init__(self, opts):
            captured.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return {"id": "1", "ext": "m4a"}

        def prepare_filename(self, data):
            return "/tmp/1.m4a"

    import sys
    import types as _types

    fake = _types.ModuleType("yt_dlp")
    fake.YoutubeDL = _FakeYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)
    monkeypatch.setattr(engine, "ytdlp_available", lambda: True, raising=False)
    monkeypatch.setattr(
        "navig_download.tiktok.ytdlp_available", lambda: True, raising=False
    )

    try:
        engine.fetch_file("https://www.tiktok.com/@x/video/1", audio_only=True)
    except Exception:  # noqa: BLE001 — only the chosen format matters here
        pass

    assert "bestaudio" in captured.get("format", ""), captured.get("format")


# ── transcript enrichment: pay for it only when the caption says nothing ──────


@pytest.mark.parametrize("description", [
    "", "   ", "#ВэтотДень", "#fyp #viral #foryou", "@someone", "🔥🔥",
])
def test_thin_descriptions_are_detected(description):
    assert engine.description_is_thin({"description": description}) is True


@pytest.mark.parametrize("description", [
    "Here is how I rebuilt the engine bay in one weekend",
    "three tips for better sourdough",
])
def test_real_prose_is_not_thin(description):
    assert engine.description_is_thin({"description": description}) is False


def test_missing_description_is_thin():
    assert engine.description_is_thin({}) is True
    assert engine.description_is_thin(None) is True


async def test_thin_description_pulls_the_spoken_transcript(monkeypatch):
    """The bare-hashtag case: without speech the briefing describes the reaction
    to a video without ever describing the video."""
    calls = []

    monkeypatch.setattr(
        engine, "info_with_comments",
        lambda url, **kw: {"description": "#ВэтотДень", "comments": [{"text": "x", "likes": 1}]},
    )
    monkeypatch.setattr(engine, "brief_meta", _capture_brief(calls))

    async def _spoken():
        return "the words actually said in the clip"

    out = await engine.analyse("u", browser_comments=False, get_transcript=_spoken)

    assert calls[0]["transcript"] == "the words actually said in the clip"
    assert out["used_transcript"] is True


async def test_a_real_caption_skips_the_expensive_step(monkeypatch):
    calls = []
    invoked = []

    monkeypatch.setattr(
        engine, "info_with_comments",
        lambda url, **kw: {"description": "Here is how I rebuilt the engine bay",
                           "comments": [{"text": "x", "likes": 1}]},
    )
    monkeypatch.setattr(engine, "brief_meta", _capture_brief(calls))

    async def _spoken():
        invoked.append(1)
        return "unused"

    out = await engine.analyse("u", browser_comments=False, get_transcript=_spoken)

    assert invoked == [], "a video with a real caption must not pay for transcription"
    assert calls[0]["transcript"] is None
    assert out["used_transcript"] is False


async def test_a_failing_transcript_never_breaks_the_briefing(monkeypatch):
    calls = []
    monkeypatch.setattr(
        engine, "info_with_comments", lambda url, **kw: {"description": "#tag"},
    )
    monkeypatch.setattr(engine, "brief_meta", _capture_brief(calls))

    async def _boom():
        raise RuntimeError("no stt configured")

    out = await engine.analyse("u", browser_comments=False, get_transcript=_boom)

    assert out["brief"] == "BRIEF", "the briefing must still be produced"
    assert out["used_transcript"] is False


async def test_an_explicit_transcript_wins_and_skips_the_callback(monkeypatch):
    calls = []
    invoked = []
    monkeypatch.setattr(
        engine, "info_with_comments", lambda url, **kw: {"description": "#tag"},
    )
    monkeypatch.setattr(engine, "brief_meta", _capture_brief(calls))

    async def _spoken():
        invoked.append(1)
        return "lazy"

    await engine.analyse("u", browser_comments=False,
                         transcript="given", get_transcript=_spoken)

    assert invoked == []
    assert calls[0]["transcript"] == "given"


def _capture_brief(sink):
    async def _brief(meta, *, language=None, transcript=None):
        sink.append({"language": language, "transcript": transcript})
        return "BRIEF"

    return _brief


# ── an age gate is not a bot-wall ────────────────────────────────────────────
#
# Reported from vm.tiktok.com/ZGdxAM5DG. yt-dlp says: "This post may not be
# comfortable for some audiences. Log in for access." _BLOCK_MARKERS already
# carried "sign in" and "login required" — and that message contains NEITHER
# ("log in" is not "login"), so it fell through to the generic branch and the bot
# said only "Couldn't read that video."

_REAL_GATE_MESSAGE = (
    "ERROR: [TikTok] 7671955756333255957: This post may not be comfortable for "
    "some audiences. Log in for access. Use --cookies-from-browser or --cookies "
    "for the authentication."
)


def test_the_real_gate_message_is_classified_as_login_required():
    from navig_download.anti_detect import looks_login_required

    assert looks_login_required(_REAL_GATE_MESSAGE)


def test_the_raise_site_prefers_the_narrower_class():
    """A bot-wall is transient and an age gate is not, so "try again shortly"
    sends the operator off retrying something that can never succeed."""
    import pytest

    with pytest.raises(engine.TikTokLoginRequired):
        engine._raise_for_extractor_error(RuntimeError(_REAL_GATE_MESSAGE))


def test_a_genuine_bot_wall_is_still_a_bot_wall():
    import pytest

    with pytest.raises(engine.TikTokBlocked):
        engine._raise_for_extractor_error(RuntimeError("HTTP Error 429: Too Many Requests"))


def test_an_ordinary_failure_is_left_alone():
    """Neither class fits a deleted post — it must reach the generic handler."""
    engine._raise_for_extractor_error(RuntimeError("Unable to download webpage: 404"))


# ── an unreadable response is not a bot-wall ─────────────────────────────────

_REAL_UNREADABLE_MESSAGE = (
    "ERROR: [TikTok] 7652338755679964436: Unexpected response from webpage request; "
    "please report this issue on https://github.com/yt-dlp/yt-dlp/issues?q= , filling "
    "out the appropriate issue template. Confirm you are on the latest version using "
    "yt-dlp -U"
)


def test_the_real_unreadable_message_is_classified():
    from navig_download.anti_detect import looks_unreadable_response

    assert looks_unreadable_response(_REAL_UNREADABLE_MESSAGE)


def test_it_is_classified_without_stealing_the_bot_wall_path():
    """The stale class must not claim an ambiguous phrasing whose bot-wall
    handling escalates to the browser tier and often succeeds."""
    import pytest

    with pytest.raises(engine.TikTokUnreadableResponse):
        engine._raise_for_extractor_error(RuntimeError(_REAL_UNREADABLE_MESSAGE))
    # …but "unable to extract webpage video data" stays a BOT-WALL: it is equally
    # a block, and that path escalates to the browser tier, which often succeeds.
    with pytest.raises(engine.TikTokBlocked):
        engine._raise_for_extractor_error(
            RuntimeError("ERROR: Unable to extract webpage video data"))


def test_a_login_gate_still_wins_over_an_unreadable_response():
    """Both can appear in one message; the gate is the narrower, actionable claim."""
    import pytest

    with pytest.raises(engine.TikTokLoginRequired):
        engine._raise_for_extractor_error(RuntimeError(
            "Log in for access. Confirm you are on the latest version using yt-dlp -U"))


# ── when yt-dlp is refused, the SSR reader still has the card ────────────────
#
# Observed live: `info()` raised for a post whose images `download_post_images`
# fetched seconds later through `read_post_http` — so the card went bare while its
# content sat one function away in the same module. The bot showed
# "🎵 TikTok link" and nothing else.

_HTTP_META = {
    "id": "765", "uploader": "get.man_", "description": "a real caption",
    "view_count": 23800, "like_count": 866, "comment_count": 12, "repost_count": 3,
    "images": ["https://cdn/1.jpg", "https://cdn/2.jpg"], "is_photo": True,
}


def _ydl_refuses(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("ERROR: [TikTok] 765: Unexpected response from webpage request")
    monkeypatch.setattr(engine, "_ydl", _boom)


def test_info_serves_the_card_from_http_when_ytdlp_is_refused(monkeypatch):
    engine._info_cache.clear()
    _ydl_refuses(monkeypatch)
    monkeypatch.setattr(engine, "read_post_http", lambda url, **kw: dict(_HTTP_META))

    out = engine.info("https://www.tiktok.com/@get.man_/photo/765")

    assert out["description"] == "a real caption"
    assert out["like_count"] == 866
    # the first slide becomes the cover, so a photo post still gets its picture
    assert out["thumbnail"] == "https://cdn/1.jpg"
    assert out["is_photo"] is True and out["has_video"] is False
    assert engine.render_card(out), "the card renders from the fallback"


def test_a_useless_blob_lets_the_REAL_error_through(monkeypatch):
    """Inventing an empty summary would replace a real failure with a card that
    says nothing — the exact shape this module keeps closing."""
    import pytest

    engine._info_cache.clear()
    _ydl_refuses(monkeypatch)
    monkeypatch.setattr(engine, "read_post_http",
                        lambda url, **kw: {"id": "765", "images": [], "description": ""})

    with pytest.raises(engine.TikTokUnreadableResponse):
        engine.info("https://www.tiktok.com/@get.man_/photo/765")


def test_a_failing_fallback_never_masks_the_original_failure(monkeypatch):
    import pytest

    engine._info_cache.clear()
    _ydl_refuses(monkeypatch)

    def _also_boom(*a, **kw):
        raise RuntimeError("http reader down too")

    monkeypatch.setattr(engine, "read_post_http", _also_boom)

    with pytest.raises(engine.TikTokUnreadableResponse):
        engine.info("https://www.tiktok.com/@get.man_/photo/765")


def test_the_fallback_is_cached_like_any_other_summary(monkeypatch):
    """Otherwise every button re-reads tiktok.com on exactly the posts where it is
    already refusing us."""
    engine._info_cache.clear()
    _ydl_refuses(monkeypatch)
    calls = []

    def _read(url, **kw):
        calls.append(url)
        return dict(_HTTP_META)

    monkeypatch.setattr(engine, "read_post_http", _read)
    url = "https://www.tiktok.com/@get.man_/photo/765"

    first = engine.info(url)
    second = engine.info(url)

    assert len(calls) == 1, f"the fallback was not cached ({len(calls)} reads)"
    first["description"] = "mutated by a caller"
    assert engine.info(url)["description"] == "a real caption", "the cache was poisoned"
    assert second["description"] == "a real caption"


# ── …and the SAME is true of the reader the BRIEFING uses ────────────────────
#
# The fallback above was added to `info()` and its twin `info_with_comments()`
# was left raising. `analyse()` is that twin's only caller, so 🔍 Analyse was the
# one action in the whole feature that could not survive a yt-dlp refusal — while
# the card, 📄 Full text (both `info`) and ⬇️/🎧/📝 (the browser rescue in
# `fetch_file`) all served the same post seconds apart. Measured live on
# https://www.tiktok.com/@timmartynov/video/7649666940612496660: five actions
# succeeded, 🔍 printed "update the downloader" in 2.6s.


def _http_video_meta() -> dict:
    """A refused VIDEO post as the SSR reader sees it: stats, no comments."""
    return {
        "id": "764", "uploader": "timmartynov", "description": "a real caption",
        "view_count": 283200, "like_count": 8552, "comment_count": 812,
        "repost_count": 1504, "images": [],
    }


def test_the_briefing_reader_survives_a_refusal_exactly_like_info(monkeypatch):
    _ydl_refuses(monkeypatch)
    monkeypatch.setattr(engine, "read_post_http", lambda url, **kw: _http_video_meta())

    out = engine.info_with_comments("https://www.tiktok.com/@timmartynov/video/764")

    assert out["description"] == "a real caption"
    assert out["comment_count"] == 812


def test_a_refusal_lands_in_the_state_that_ESCALATES_to_the_browser(monkeypatch):
    """The HTTP reader carries the stats but never the comments — which is what
    `comments_blocked` means. `analyse()` keys its browser escalation on exactly
    that, so the recovery path that was written and tested for the soft block now
    also covers a refusal, instead of being unreachable behind a raise."""
    _ydl_refuses(monkeypatch)
    monkeypatch.setattr(engine, "read_post_http", lambda url, **kw: _http_video_meta())

    out = engine.info_with_comments("https://www.tiktok.com/@timmartynov/video/764")

    assert out["comments"] == []
    assert out["comments_blocked"] is True, "the browser escalation stays unreachable"


def test_a_post_with_no_comments_is_not_reported_as_gated(monkeypatch):
    """"Comments were gated" printed above a briefing that is missing nothing is a
    false claim — the same honesty rule the yt-dlp path already follows."""
    _ydl_refuses(monkeypatch)
    meta = _http_video_meta() | {"comment_count": 0}
    monkeypatch.setattr(engine, "read_post_http", lambda url, **kw: meta)

    out = engine.info_with_comments("https://www.tiktok.com/@timmartynov/video/764")

    assert out["comments_blocked"] is False


def test_the_briefing_reader_still_raises_when_the_blob_is_useless_too(monkeypatch):
    """A fallback that invents an empty summary would replace a real failure with a
    briefing about nothing."""
    import pytest

    _ydl_refuses(monkeypatch)
    monkeypatch.setattr(engine, "read_post_http",
                        lambda url, **kw: {"id": "764", "images": [], "description": ""})

    with pytest.raises(engine.TikTokUnreadableResponse):
        engine.info_with_comments("https://www.tiktok.com/@timmartynov/video/764")


def test_neither_reader_can_lose_the_fallback_again():
    """Both readers must go through the ONE shared refusal tail.

    This is the guard the original fix needed and did not have: hardening a path
    while its twin stays open is how 🔍 Analyse ended up alone. A third reader
    added later fails here until it takes the same route.
    """
    import inspect

    for name in ("info", "info_with_comments"):
        src = inspect.getsource(getattr(engine, name))
        assert "_http_fallback_or_raise" in src, (
            f"{name}() does not take the shared SSR fallback — a yt-dlp refusal "
            f"will surface to the user as a hard failure"
        )


# ── a refusal the user sees must be a refusal the LOG saw ────────────────────


def test_a_refusal_is_recorded_at_warning(caplog):
    """`_raise_for_extractor_error` is the one place every classified TikTok
    failure is born, and both callers reach it only once every fallback is spent.
    It was silent — so "🔧 could not read" in Telegram could not be traced to a
    post or a cause without reproducing it live."""
    import logging

    import pytest

    with caplog.at_level(logging.WARNING, logger="navig_download.tiktok.engine"):
        with pytest.raises(engine.TikTokUnreadableResponse):
            engine._raise_for_extractor_error(RuntimeError(_REAL_UNREADABLE_MESSAGE))

    assert "TikTokUnreadableResponse" in caplog.text
    assert "7652338755679964436" in caplog.text, "the post id makes it actionable"


def test_every_classified_refusal_is_recorded(caplog):
    """All three, not just the one that prompted this — a class that logs for one
    of its members is the same blind spot one size smaller."""
    import logging

    import pytest

    cases = [
        ("Log in for access", engine.TikTokLoginRequired),
        (_REAL_UNREADABLE_MESSAGE, engine.TikTokUnreadableResponse),
        ("ERROR: Unable to extract webpage video data", engine.TikTokBlocked),
    ]
    for message, expected in cases:
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="navig_download.tiktok.engine"):
            with pytest.raises(expected):
                engine._raise_for_extractor_error(RuntimeError(message))
        assert expected.__name__ in caplog.text, f"{expected.__name__} was refused silently"


def test_an_unclassified_error_is_left_alone(caplog):
    """It does not raise (the caller re-raises the original), and it must not
    claim a classification it did not make."""
    import logging

    with caplog.at_level(logging.WARNING, logger="navig_download.tiktok.engine"):
        engine._raise_for_extractor_error(RuntimeError("something else entirely"))

    assert "refused as" not in caplog.text
