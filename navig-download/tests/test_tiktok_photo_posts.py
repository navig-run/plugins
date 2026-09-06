"""TikTok **photo posts** — the format yt-dlp's extractor cannot address.

Reported by the operator: sharing ``https://vm.tiktok.com/ZGdxB8H3o/`` with the
bot produced a card reading only "🎵 TikTok link" (no description at all) and
"Couldn't extract the audio." on the 🎧 button. The daemon log named it::

    tiktok audio failed: ERROR: Unsupported URL:
    https://www.tiktok.com/@get.man_/photo/7652338755679964436?_r=1&_t=…

The share link resolves to a **photo post**, and yt-dlp's TikTok ``_VALID_URL``
matches only ``/video/`` — so *every* engine entry point raised `Unsupported URL`
for it. TikTok serves the same post id under both paths and the ``/video/`` form
returns the description, the stats and the audio track, so canonicalizing the URL
at the yt-dlp boundary fixes metadata, briefing, transcript and audio at once.

What it does NOT fix is downloading: a slideshow has no video stream, and the
format ladder ends in a bare ``best`` — so a request for video would quietly
return the AUDIO track and hand the user an unplayable file, reported as a
successful download. That is what :class:`TikTokNoVideo` exists to stop.
"""
from __future__ import annotations

import pytest

from navig_download.tiktok import engine

_PHOTO = "https://www.tiktok.com/@get.man_/photo/7652338755679964436"
_VIDEO = "https://www.tiktok.com/@get.man_/video/7652338755679964436"
_SHORT = "https://vm.tiktok.com/ZGdxB8H3o/"

#: A photo post as yt-dlp reports it: ONE format, and it is the audio track.
_PHOTO_INFO = {
    "id": "7652338755679964436",
    "title": "Мы привыкли думать, что интуиция — это магия",
    "description": "Мы привыкли думать, что интуиция — это магия",
    "uploader": "get.man_",
    "webpage_url": _VIDEO,
    "view_count": 22400,
    "formats": [{"format_id": "audio", "ext": "m4a", "vcodec": "none", "acodec": "aac"}],
}

_VIDEO_INFO = {
    "id": "1", "title": "t", "description": "d", "uploader": "u",
    "webpage_url": _VIDEO,
    "formats": [{"format_id": "h264_540p", "ext": "mp4", "vcodec": "h264", "acodec": "aac"}],
}


# ── the rewrite itself ────────────────────────────────────────────────────────


class TestCanonicalUrl:
    def test_photo_path_is_rewritten_to_video(self):
        """The one-line root cause: yt-dlp's _VALID_URL matches only /video/."""
        assert engine.as_video_url(_PHOTO) == _VIDEO

    def test_a_video_url_is_untouched(self):
        assert engine.as_video_url(_VIDEO) == _VIDEO

    def test_a_username_with_dots_and_underscores_survives(self):
        """`@get.man_` is the account that produced the report — a naive `\\w+`
        pattern stops at the dot and rewrites nothing."""
        assert "/video/" in engine.as_video_url(_PHOTO)
        assert "@get.man_" in engine.as_video_url(_PHOTO)

    def test_only_the_post_path_is_rewritten(self):
        """A `/photo/` appearing elsewhere in the URL must not be touched."""
        url = "https://www.tiktok.com/@a/video/12?ref=/photo/9"
        assert engine.as_video_url(url) == url

    def test_a_full_url_needs_no_network_to_canonicalize(self, monkeypatch):
        """Resolution is for share links ONLY. Paying a redirect round-trip for
        an ordinary link would tax every video the bot ever sees."""
        def _boom(*a, **kw):
            raise AssertionError("resolve_url must not do I/O for a full URL")

        monkeypatch.setattr(engine, "_final_url", _boom)
        assert engine.canonical_url(_VIDEO) == _VIDEO
        assert engine.canonical_url(_PHOTO) == _VIDEO

    def test_share_tracking_params_are_stripped(self):
        assert engine.resolve_url(_VIDEO + "?_r=1&_t=ZG-98goCRbvGmP") == _VIDEO

    def test_short_links_are_recognised(self):
        for short in (_SHORT, "https://vt.tiktok.com/ZS123/", "https://www.tiktok.com/t/ZS1/"):
            assert engine.is_short_url(short) is True
        assert engine.is_short_url(_VIDEO) is False
        assert engine.is_short_url(None) is False


class TestResolveIsBestEffort:
    def setup_method(self):
        engine._resolved.clear()

    def test_a_failed_resolution_returns_the_input(self, monkeypatch):
        """yt-dlp resolves share links itself, so a redirect we could not follow
        must never become a failure the caller reports."""
        monkeypatch.setattr(
            engine, "_final_url",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("network down")))
        assert engine.resolve_url(_SHORT) == _SHORT

    def test_a_resolution_is_cached(self, monkeypatch):
        """One shared link is read up to five times — the card plus four buttons."""
        calls = []
        monkeypatch.setattr(
            engine, "_final_url", lambda u, **kw: calls.append(u) or _PHOTO)
        assert engine.resolve_url(_SHORT) == _PHOTO
        assert engine.resolve_url(_SHORT) == _PHOTO
        assert len(calls) == 1, "each button must not pay its own redirect"

    def test_the_cache_is_bounded(self, monkeypatch):
        monkeypatch.setattr(engine, "_final_url", lambda u, **kw: _PHOTO)
        for i in range(engine._RESOLVE_MAX + 5):
            engine.resolve_url(f"https://vm.tiktok.com/Z{i}/")
        assert len(engine._resolved) <= engine._RESOLVE_MAX


# ── the entry points now read a photo post ────────────────────────────────────


class _FakeYDL:
    """Records the URL it was handed — the thing the regression is about."""

    seen: list[str] = []

    def __init__(self, data):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        type(self).seen.append(url)
        return self._data


@pytest.fixture
def ydl_sees(monkeypatch):
    _FakeYDL.seen = []

    def _install(data):
        monkeypatch.setattr(engine, "_ydl", lambda **kw: _FakeYDL(data))
        return _FakeYDL.seen

    return _install


class TestInfoReadsPhotoPosts:
    def test_info_hands_ytdlp_the_video_form(self, ydl_sees):
        seen = ydl_sees(_PHOTO_INFO)
        engine.info(_PHOTO)
        assert seen == [_VIDEO], "a /photo/ URL reaching yt-dlp is `Unsupported URL`"

    def test_info_returns_the_description(self, ydl_sees):
        ydl_sees(_PHOTO_INFO)
        meta = engine.info(_PHOTO)
        assert meta["description"].startswith("Мы привыкли"), (
            "the card showed a bare '🎵 TikTok link' because this was an exception"
        )

    def test_info_flags_the_post_as_a_slideshow(self, ydl_sees):
        ydl_sees(_PHOTO_INFO)
        meta = engine.info(_PHOTO)
        assert meta["is_photo"] is True
        assert meta["has_video"] is False

    def test_the_real_photo_url_is_kept_for_display(self, ydl_sees):
        """yt-dlp reports the REWRITTEN url back to us; 'open on TikTok' should
        point at the post the operator actually shared."""
        ydl_sees(_PHOTO_INFO)
        assert engine.info(_PHOTO)["url"] == _PHOTO

    def test_an_ordinary_video_is_unaffected(self, ydl_sees):
        seen = ydl_sees(_VIDEO_INFO)
        meta = engine.info(_VIDEO)
        assert seen == [_VIDEO]
        assert meta["is_photo"] is False
        assert meta["has_video"] is True
        assert meta["url"] == _VIDEO

    def test_comments_path_is_canonicalized_too(self, ydl_sees):
        seen = ydl_sees({**_PHOTO_INFO, "comments": [], "comment_count": 0})
        out = engine.info_with_comments(_PHOTO)
        assert seen == [_VIDEO]
        assert out["is_photo"] is True


class TestSoundMetadata:
    """The SOUND's performer is not the poster.

    "Veins of Sand" is by GTMN, shared by @get.man_. Without `artists` the audio
    upload labels the uploader as the artist, which is wrong on every reposted
    sound — and reposted sound is most of TikTok.
    """

    def test_artists_are_projected(self, ydl_sees):
        ydl_sees({**_PHOTO_INFO, "track": "Veins of Sand", "artists": ["GTMN"]})
        assert engine.info(_PHOTO)["artists"] == ["GTMN"]

    def test_a_single_artist_field_is_accepted_too(self, ydl_sees):
        ydl_sees({**_PHOTO_INFO, "artist": "GTMN"})
        assert engine.info(_PHOTO)["artists"] == ["GTMN"]

    def test_no_artist_is_an_empty_list_not_a_none(self, ydl_sees):
        """A caller iterating it must not have to null-check first."""
        ydl_sees(_PHOTO_INFO)
        assert engine.info(_PHOTO)["artists"] == []


class TestInfoCache:
    """One shared link is read up to four times — the card, then 🎧, 📄 and 🔍.

    Each read was its own request to TikTok, which is the same "another chance to
    look like a bot" concern `_resolved` already exists for, one level up.
    """

    def test_a_second_read_costs_no_request(self, ydl_sees):
        seen = ydl_sees(_PHOTO_INFO)
        engine.info(_PHOTO)
        engine.info(_PHOTO)
        engine.info(_PHOTO)
        assert seen == [_VIDEO], "the card and every button re-fetched the same post"

    def test_refresh_bypasses_it(self, ydl_sees):
        seen = ydl_sees(_PHOTO_INFO)
        engine.info(_PHOTO)
        engine.info(_PHOTO, refresh=True)
        assert len(seen) == 2

    def test_a_share_link_and_its_canonical_url_are_the_same_post(self, monkeypatch,
                                                                  ydl_sees):
        """The card resolves the share link; a button may hold either form. Keying
        on the raw string would cache the same post twice."""
        monkeypatch.setattr(engine, "_final_url", lambda *a, **kw: _PHOTO)
        seen = ydl_sees(_PHOTO_INFO)
        engine.info(_SHORT)
        engine.info(_PHOTO)
        assert seen == [_VIDEO]

    def test_different_credentials_are_different_entries(self, ydl_sees):
        """A read through someone's logged-in cookies is not interchangeable with
        an anonymous one."""
        seen = ydl_sees(_PHOTO_INFO)
        engine.info(_PHOTO)
        engine.info(_PHOTO, cookiefile="/tmp/ck.txt")
        assert len(seen) == 2

    def test_a_caller_cannot_poison_the_cache_on_the_way_IN(self, ydl_sees):
        """The briefing path mutates what it gets back (it adds `comments`). The
        first read is a cache MISS and returns the freshly-built dict — so the
        cache has to store a copy, or that caller's edit becomes the entry."""
        ydl_sees(_PHOTO_INFO)
        first = engine.info(_PHOTO)          # miss — the object just built
        first["description"] = "TAMPERED"
        first["comments"] = ["injected"]
        second = engine.info(_PHOTO)         # hit
        assert second["description"] != "TAMPERED"
        assert "comments" not in second

    def test_a_caller_cannot_poison_the_cache_on_the_way_OUT(self, ydl_sees):
        """And the symmetric half, which the IN test cannot see: a caller holding a
        cache HIT must not be holding the entry itself. Storing a copy protects
        miss→hit; only handing out a copy protects hit→hit."""
        ydl_sees(_PHOTO_INFO)
        engine.info(_PHOTO)                  # miss — populates
        second = engine.info(_PHOTO)         # hit
        second["description"] = "TAMPERED"
        second["comments"] = ["injected"]
        third = engine.info(_PHOTO)          # hit again
        assert third["description"] != "TAMPERED"
        assert "comments" not in third

    def test_an_expired_entry_is_refetched(self, ydl_sees, monkeypatch):
        seen = ydl_sees(_PHOTO_INFO)
        engine.info(_PHOTO)
        # Age every entry past the TTL rather than sleeping for ten minutes.
        for k, (v, _exp) in list(engine._info_cache.items()):
            engine._info_cache[k] = (v, 0.0)
        engine.info(_PHOTO)
        assert len(seen) == 2

    def test_a_failed_read_is_not_cached(self, monkeypatch):
        """Caching a bot-wall would make one 403 last ten minutes."""
        monkeypatch.setattr(engine, "_ydl",
                            lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            engine.info(_PHOTO)
        assert engine._info_cache == {}

    def test_the_cache_is_bounded(self, ydl_sees):
        ydl_sees(_PHOTO_INFO)
        for i in range(engine._INFO_MAX + 5):
            engine.info(f"https://www.tiktok.com/@u/video/{i}")
        assert len(engine._info_cache) <= engine._INFO_MAX


class TestHasVideo:
    def test_a_lone_audio_format_means_no_video(self):
        assert engine._has_video(_PHOTO_INFO) is False

    def test_a_video_format_means_video(self):
        assert engine._has_video(_VIDEO_INFO) is True

    def test_a_mixed_format_list_means_video(self):
        assert engine._has_video({"formats": [
            {"vcodec": "none"}, {"vcodec": "h264"},
        ]}) is True

    def test_no_evidence_assumes_video(self):
        """Absent format data is 'unknown', and the overwhelmingly common post is
        a video — guessing 'photo' here would route ordinary clips to the
        slideshow path."""
        assert engine._has_video({}) is True
        assert engine._has_video({"id": "1"}) is True


# ── downloading: a slideshow must not be served as a video ────────────────────


def _install_fake_ytdlp(monkeypatch, data):
    """Patch yt_dlp itself, so `fetch_file`'s own download path is exercised."""
    import sys
    import types

    class _DL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return data

        def prepare_filename(self, d):
            return "/tmp/x.m4a"

    fake = types.ModuleType("yt_dlp")
    fake.YoutubeDL = _DL
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)
    monkeypatch.setattr("navig_download.tiktok.ytdlp_available", lambda: True,
                        raising=False)


class TestFetchFileRefusesToFakeAVideo:
    def test_requesting_video_from_a_slideshow_raises(self, monkeypatch, tmp_path):
        """The format ladder ends in a bare `best`, so a photo post silently
        satisfies a video request WITH ITS AUDIO TRACK — an unplayable file
        delivered as a successful download."""
        _install_fake_ytdlp(monkeypatch, _PHOTO_INFO)
        with pytest.raises(engine.TikTokNoVideo) as exc:
            engine.fetch_file(_PHOTO, dest_dir=str(tmp_path))
        assert exc.value.meta.get("is_photo") is True, (
            "the caller needs the metadata to serve the slides without refetching"
        )

    def test_audio_only_is_exactly_what_a_photo_post_can_give(self, monkeypatch, tmp_path):
        """The reported failure: 🎧 Audio. A slideshow HAS an audio track."""
        _install_fake_ytdlp(monkeypatch, _PHOTO_INFO)
        assert engine.fetch_file(_PHOTO, dest_dir=str(tmp_path), audio_only=True)

    def test_an_ordinary_video_download_still_works(self, monkeypatch, tmp_path):
        _install_fake_ytdlp(monkeypatch, _VIDEO_INFO)
        assert engine.fetch_file(_VIDEO, dest_dir=str(tmp_path))


# ── the card ──────────────────────────────────────────────────────────────────


class TestRenderCard:
    def test_the_reported_caption_lands_whole(self):
        """1939 characters — the post from the report. It fits inside Telegram's
        4096 ceiling, so a card that cuts it is losing text for no reason (the
        first fix used a flat 1500 and dropped a fifth of it)."""
        desc = "и" * 1939
        card = engine.render_card({"uploader": "x", "description": desc})
        assert desc in card
        assert not engine.card_truncates_description({"uploader": "x", "description": desc})

    def test_the_card_still_fits_one_telegram_message(self):
        """A split card puts its buttons on a second bubble, detached from the
        content they act on."""
        meta = {"uploader": "x", "description": "y" * 20_000,
                "view_count": 22_400, "like_count": 792,
                "comment_count": 37, "repost_count": 202}
        assert len(engine.render_card(meta)) <= engine._TG_MESSAGE_LIMIT

    def test_the_header_and_stats_come_out_of_the_description_budget(self):
        """A fixed description limit would let a long header push the card over."""
        long_desc = "y" * 20_000
        bare = engine.render_card({"uploader": "x", "description": long_desc})
        loaded = engine.render_card({
            "uploader": "a-considerably-longer-creator-name", "country": "Latvia",
            "is_photo": True, "description": long_desc,
            "view_count": 1_234_567, "like_count": 234_567,
            "comment_count": 34_567, "repost_count": 4_567})
        assert len(loaded) <= engine._TG_MESSAGE_LIMIT
        assert len(bare) <= engine._TG_MESSAGE_LIMIT

    def test_truncation_is_marked(self):
        """A cut description that looks complete is a silent truncation."""
        card = engine.render_card({"uploader": "x", "description": "y" * 20_000})
        assert card.endswith("…")
        assert engine.card_truncates_description({"uploader": "x",
                                                  "description": "y" * 20_000})

    def test_a_cut_never_severs_an_html_entity(self):
        """The trim is applied to the RAW text and re-measured after escaping —
        cutting the escaped string could leave `&am` and break the markup."""
        card = engine.render_card({"uploader": "x", "description": "<&>" * 8000})
        assert "&am" not in card.replace("&amp;", "")
        assert "&l" not in card.replace("&lt;", "").replace("&#x27;", "")

    def test_the_overflow_signal_agrees_with_the_card(self):
        """They share _card_parts, so the button and the text cannot disagree."""
        for n in (10, 1939, 4000, 20_000):
            meta = {"uploader": "x", "description": "z" * n}
            assert engine.card_truncates_description(meta) is engine.render_card(
                meta).endswith("…")

    def test_a_short_description_gets_no_ellipsis(self):
        card = engine.render_card({"uploader": "x", "description": "all of it"})
        assert "…" not in card

    def test_a_photo_post_says_so(self):
        """It explains why ⬇️ returns slides rather than a clip."""
        card = engine.render_card({"uploader": "x", "is_photo": True})
        assert "photo post" in card

    def test_a_video_card_is_unchanged(self):
        card = engine.render_card({"uploader": "x", "description": "d"})
        assert "photo post" not in card
        assert card.startswith("\U0001f3b5")


# ── slides: the one thing the rewrite does NOT give ───────────────────────────


_SSR = """<html><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">
{"__DEFAULT_SCOPE__": {"webapp.app-context": {"x": 1}, "webapp.video-detail":
{"itemInfo": {"itemStruct": {"id": "77", "desc": "the caption",
"author": {"uniqueId": "get.man_", "nickname": "N"},
"statsV2": {"diggCount": "791", "playCount": "22400"},
"imagePost": {"images": [
  {"imageURL": {"urlList": ["https://cdn/1.jpg"]}},
  {"imageURL": {"urlList": ["https://cdn/2.jpg"]}}]}}}}}}
</script></html>"""


class TestReadPostHttp:
    def test_slides_are_read_without_a_browser(self, monkeypatch):
        """yt-dlp reports only the cover thumbnail, and the browser tier costs a
        Patchright launch — the SSR blob has every slide."""
        monkeypatch.setattr(engine, "_get_html", lambda url, **kw: _SSR)
        meta = engine.read_post_http(_PHOTO)
        assert meta["images"] == ["https://cdn/1.jpg", "https://cdn/2.jpg"]
        assert meta["description"] == "the caption"
        assert meta["is_photo"] is True
        assert meta["like_count"] == 791

    def test_it_reads_the_canonical_url(self, monkeypatch):
        """TikTok server-renders the struct for the /video/ form and NOT for
        /photo/ — the same asymmetry canonical_url exists for."""
        seen = []
        monkeypatch.setattr(engine, "_get_html",
                            lambda url, **kw: seen.append(url) or _SSR)
        engine.read_post_http(_PHOTO)
        assert seen == [_VIDEO]

    def test_a_missing_blob_is_none_not_an_empty_post(self, monkeypatch):
        """'Could not read it' and 'read it, nothing there' are different answers."""
        monkeypatch.setattr(engine, "_get_html", lambda url, **kw: "<html></html>")
        assert engine.read_post_http(_PHOTO) is None

    def test_a_broken_blob_is_none(self, monkeypatch):
        monkeypatch.setattr(
            engine, "_get_html",
            lambda url, **kw: '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">{oops</script>')
        assert engine.read_post_http(_PHOTO) is None

    def test_a_failed_fetch_is_none(self, monkeypatch):
        monkeypatch.setattr(
            engine, "_get_html",
            lambda url, **kw: (_ for _ in ()).throw(OSError("no route")))
        assert engine.read_post_http(_PHOTO) is None


class TestDownloadPostImages:
    def test_it_reports_the_total_not_just_what_it_saved(self, monkeypatch, tmp_path):
        """A cap that says nothing presents part of a post as the whole of it."""
        monkeypatch.setattr(engine, "_get_html", lambda url, **kw: _SSR)
        monkeypatch.setattr(engine, "_download_image",
                            lambda url, dest, **kw: dest.write_bytes(b"jpg"))
        saved, total = engine.download_post_images(_PHOTO, dest_dir=str(tmp_path), limit=1)
        assert len(saved) == 1
        assert total == 2

    def test_one_bad_slide_does_not_drop_the_rest(self, monkeypatch, tmp_path):
        monkeypatch.setattr(engine, "_get_html", lambda url, **kw: _SSR)

        def _flaky(url, dest, **kw):
            if url.endswith("1.jpg"):
                raise OSError("cdn hiccup")
            dest.write_bytes(b"jpg")

        monkeypatch.setattr(engine, "_download_image", _flaky)
        saved, total = engine.download_post_images(_PHOTO, dest_dir=str(tmp_path))
        assert len(saved) == 1 and total == 2

    def test_an_unreadable_post_yields_nothing_rather_than_raising(self, monkeypatch, tmp_path):
        monkeypatch.setattr(engine, "_get_html", lambda url, **kw: "<html></html>")
        assert engine.download_post_images(_PHOTO, dest_dir=str(tmp_path)) == ([], 0)
