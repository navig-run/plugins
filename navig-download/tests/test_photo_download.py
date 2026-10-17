"""Photo-carousel download: URL routing, filename safety, and the download flow (mocked)."""

from __future__ import annotations

import json

import pytest

from navig_download.tiktok import browser_fetch, engine


@pytest.fixture(autouse=True)
def _no_http_read(monkeypatch):
    """Default every case to the BROWSER path, and keep the suite off the network.

    `download_photo` now reads the SSR'd item struct over plain HTTP first, so
    without this each browser-path case below would fire a real request at
    tiktok.com before falling through. The HTTP path has its own cases, which opt
    back in explicitly.
    """
    monkeypatch.setattr(engine, "read_post_http", lambda url, **kw: None)


def test_is_photo_url():
    assert engine.is_photo_url("https://www.tiktok.com/@u/photo/123") is True
    assert engine.is_photo_url("https://www.tiktok.com/@u/video/123") is False
    assert engine.is_photo_url("") is False


def test_is_photo_url_stays_pure(monkeypatch):
    """It answers about the STRING. Making it resolve would put a network call
    behind a predicate that reads like a free one."""
    monkeypatch.setattr(engine, "_final_url",
                        lambda *a, **kw: pytest.fail("is_photo_url must not do I/O"))
    assert engine.is_photo_url("https://vm.tiktok.com/ZGdxB8H3o/") is False


class TestIsPhotoPost:
    """The resolving companion — and the one nearly every caller wants.

    A share link has no path, so the pure check calls every shared photo post a
    video and routes it into yt-dlp, which cannot read one at all.
    """

    def setup_method(self):
        engine._resolved.clear()

    def teardown_method(self):
        engine._resolved.clear()

    def test_a_share_link_to_a_photo_post_is_recognised(self, monkeypatch):
        monkeypatch.setattr(engine, "_final_url",
                            lambda *a, **kw: "https://www.tiktok.com/@u/photo/1")
        assert engine.is_photo_post("https://vm.tiktok.com/ZGdxB8H3o/") is True

    def test_a_share_link_to_a_video_is_not(self, monkeypatch):
        monkeypatch.setattr(engine, "_final_url",
                            lambda *a, **kw: "https://www.tiktok.com/@u/video/1")
        assert engine.is_photo_post("https://vm.tiktok.com/ZGdxB8H3o/") is False

    def test_a_full_url_needs_no_lookup(self, monkeypatch):
        monkeypatch.setattr(engine, "_final_url",
                            lambda *a, **kw: pytest.fail("no resolve for a full URL"))
        assert engine.is_photo_post("https://www.tiktok.com/@u/photo/1") is True
        assert engine.is_photo_post("https://www.tiktok.com/@u/video/1") is False

    def test_an_unresolvable_link_is_not_guessed_into_a_photo(self, monkeypatch):
        monkeypatch.setattr(
            engine, "_final_url",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("offline")))
        assert engine.is_photo_post("https://vm.tiktok.com/ZZZ/") is False


def test_safe_name():
    assert engine._safe_name("bob smith!") == "bob_smith"
    assert engine._safe_name("@handle.name-1") == "@handle.name-1"
    assert engine._safe_name("   ") == "tiktok"
    assert engine._safe_name("../../etc") == "etc"


def _fake_post(meta):
    async def _f(url, **kw):
        return {"meta": {**meta, "url": url}, "comments": [], "captured": 1,
                "engine": "browser", "signed_request": None}
    return _f


def test_download_photo_saves_images_and_metadata(tmp_path, monkeypatch):
    meta = {"id": "999", "uploader": "photog", "uploader_name": "Photog",
            "description": "sunset", "is_photo": True, "like_count": 40,
            "images": ["https://cdn/a.jpg", "https://cdn/b.jpg", "https://cdn/c.jpg"]}
    monkeypatch.setattr(browser_fetch, "fetch_post", _fake_post(meta))
    grabbed = []

    def fake_dl(url, dest, **kw):
        grabbed.append(url)
        dest.write_bytes(b"img-bytes")

    monkeypatch.setattr(engine, "_download_image", fake_dl)

    res = engine.download_photo("https://www.tiktok.com/@photog/photo/999",
                                output_dir=str(tmp_path))
    assert res["ok"] is True
    assert res["count"] == 3 and res["total"] == 3 and res["failed"] == 0
    assert len(grabbed) == 3
    folder = tmp_path / "photog" / "999"
    assert (folder / "01.jpg").read_bytes() == b"img-bytes"
    assert (folder / "03.jpg").exists()
    saved_meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    assert saved_meta["description"] == "sunset"
    assert saved_meta["id"] == "999"
    assert len(saved_meta["images"]) == 3


def test_download_photo_partial_failure_continues(tmp_path, monkeypatch):
    meta = {"id": "1", "uploader": "u", "images": ["a", "b", "c"], "is_photo": True}
    monkeypatch.setattr(browser_fetch, "fetch_post", _fake_post(meta))

    def flaky(url, dest, **kw):
        if url == "b":
            raise RuntimeError("cdn 403")
        dest.write_bytes(b"ok")

    monkeypatch.setattr(engine, "_download_image", flaky)
    res = engine.download_photo("https://www.tiktok.com/@u/photo/1", output_dir=str(tmp_path))
    assert res["ok"] is True          # some succeeded
    assert res["count"] == 2 and res["failed"] == 1


def test_download_photo_no_images_reports_login_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_fetch, "fetch_post",
                        _fake_post({"id": "1", "uploader": "u", "images": []}))
    res = engine.download_photo("https://www.tiktok.com/@u/photo/1", output_dir=str(tmp_path))
    assert res["ok"] is False and res["count"] == 0
    assert "login" in res["error"].lower() or "gated" in res["error"].lower()


def test_download_photo_browser_unavailable(tmp_path, monkeypatch):
    async def _boom(url, **kw):
        raise browser_fetch.TikTokBrowserUnavailable("no patchright")

    monkeypatch.setattr(browser_fetch, "fetch_post", _boom)
    res = engine.download_photo("https://www.tiktok.com/@u/photo/1", output_dir=str(tmp_path))
    assert res["ok"] is False and res["count"] == 0
    assert "patchright" in res["error"].lower()


def test_download_photo_no_metadata_flag(tmp_path, monkeypatch):
    meta = {"id": "5", "uploader": "u", "images": ["a"], "is_photo": True}
    monkeypatch.setattr(browser_fetch, "fetch_post", _fake_post(meta))
    monkeypatch.setattr(engine, "_download_image", lambda url, dest, **kw: dest.write_bytes(b"x"))
    engine.download_photo("https://www.tiktok.com/@u/photo/5", output_dir=str(tmp_path),
                          save_metadata=False)
    assert not (tmp_path / "u" / "5" / "metadata.json").exists()


# ── download_photos (shared-controller batch) ─────────────────────────────────

def test_download_photos_reuses_one_controller(tmp_path, monkeypatch):
    """A batch of >1 photo posts opens exactly ONE browser, not one per post."""
    calls = {"make": 0, "fetch": 0, "stop": 0}

    class FakeCtl:
        async def start(self):
            pass

        async def stop(self):
            calls["stop"] += 1

        @property
        def page(self):
            return None

    async def fake_make(headless, proxy, controller):
        calls["make"] += 1
        return FakeCtl(), True, "browser"

    async def fake_fetch(url, **kw):
        calls["fetch"] += 1
        assert kw.get("controller") is not None  # batch reuses the shared controller
        pid = url.rsplit("/", 1)[-1]
        return {"meta": {"id": pid, "uploader": "u", "images": ["a", "b"], "is_photo": True,
                         "url": url}, "comments": [], "captured": 1, "engine": "browser",
                "signed_request": None}

    monkeypatch.setattr(browser_fetch, "_make_controller", fake_make)
    monkeypatch.setattr(browser_fetch, "fetch_post", fake_fetch)
    monkeypatch.setattr(engine, "_download_image", lambda url, dest, **kw: dest.write_bytes(b"x"))

    urls = [f"https://www.tiktok.com/@u/photo/{i}" for i in (1, 2, 3)]
    res = engine.download_photos(urls, output_dir=str(tmp_path))
    assert calls["make"] == 1        # ONE browser for the whole batch
    assert calls["fetch"] == 3       # each post read through it
    assert calls["stop"] == 1        # closed once at the end
    assert res["count"] == 3 and res["images"] == 6
    assert (tmp_path / "u" / "1" / "01.jpg").exists()
    assert (tmp_path / "u" / "3" / "02.jpg").exists()


def test_download_photos_single_delegates(tmp_path, monkeypatch):
    """A single photo URL uses the simple path (its own browser), not the batch loop."""
    meta = {"id": "9", "uploader": "u", "images": ["a"], "is_photo": True}
    monkeypatch.setattr(browser_fetch, "fetch_post", _fake_post(meta))
    monkeypatch.setattr(engine, "_download_image", lambda url, dest, **kw: dest.write_bytes(b"x"))
    res = engine.download_photos(["https://www.tiktok.com/@u/photo/9"], output_dir=str(tmp_path))
    assert res["count"] == 1 and res["images"] == 1
    assert res["posts"][0]["url"].endswith("/photo/9")


def test_download_photos_filters_non_photos(tmp_path):
    res = engine.download_photos(["https://www.tiktok.com/@u/video/1"], output_dir=str(tmp_path))
    assert res == {"ok": True, "posts": [], "count": 0, "images": 0}


def test_download_photos_one_bad_post_continues(tmp_path, monkeypatch):
    calls = {"n": 0}

    class FakeCtl:
        async def start(self):
            pass

        async def stop(self):
            pass

        @property
        def page(self):
            return None

    async def fake_make(headless, proxy, controller):
        return FakeCtl(), True, "browser"

    async def fake_fetch(url, **kw):
        calls["n"] += 1
        if url.endswith("/2"):
            raise RuntimeError("boom on post 2")
        return {"meta": {"id": url[-1], "uploader": "u", "images": ["a"], "is_photo": True,
                         "url": url}, "comments": [], "captured": 1, "engine": "browser",
                "signed_request": None}

    monkeypatch.setattr(browser_fetch, "_make_controller", fake_make)
    monkeypatch.setattr(browser_fetch, "fetch_post", fake_fetch)
    monkeypatch.setattr(engine, "_download_image", lambda url, dest, **kw: dest.write_bytes(b"x"))

    urls = [f"https://www.tiktok.com/@u/photo/{i}" for i in (1, 2, 3)]
    res = engine.download_photos(urls, output_dir=str(tmp_path))
    assert calls["n"] == 3           # all 3 attempted despite #2 failing
    assert res["count"] == 2         # 1 and 3 saved
    bad = [p for p in res["posts"] if p["url"].endswith("/2")][0]
    assert bad["ok"] is False and "boom" in bad["error"]


# ── HTTP first, browser second ────────────────────────────────────────────────
#
# TikTok server-renders the whole carousel into the page, so a slideshow can be
# read with one request. Before this, every photo download launched Patchright —
# ~10s slower, and impossible at all on an install with no browser engine.


def _http_meta(n_images: int = 2, **over) -> dict:
    return {"id": "77", "uploader": "photog", "uploader_name": "Photog",
            "description": "read over http", "is_photo": True,
            "images": [f"https://cdn/{i}.jpg" for i in range(1, n_images + 1)], **over}


class TestDownloadPhotoPrefersHttp:
    def test_the_browser_is_not_launched_when_http_can_read_it(self, tmp_path, monkeypatch):
        async def _no_browser(url, **kw):
            pytest.fail("the browser must not be launched")

        monkeypatch.setattr(engine, "read_post_http", lambda url, **kw: _http_meta(2))
        monkeypatch.setattr(browser_fetch, "fetch_post", _no_browser)
        monkeypatch.setattr(engine, "_download_image",
                            lambda url, dest, **kw: dest.write_bytes(b"img"))

        res = engine.download_photo("https://www.tiktok.com/@photog/photo/77",
                                    output_dir=str(tmp_path))
        assert res["ok"] is True and res["count"] == 2
        assert (tmp_path / "photog" / "77" / "01.jpg").read_bytes() == b"img"

    def test_it_falls_back_to_the_browser_when_the_page_was_not_rendered(
            self, tmp_path, monkeypatch):
        """A login-gated post has no SSR blob — that is what the browser is for."""
        monkeypatch.setattr(engine, "read_post_http", lambda url, **kw: None)
        monkeypatch.setattr(browser_fetch, "fetch_post",
                            _fake_post(_http_meta(1, description="via browser")))
        monkeypatch.setattr(engine, "_download_image",
                            lambda url, dest, **kw: dest.write_bytes(b"img"))

        res = engine.download_photo("https://www.tiktok.com/@photog/photo/77",
                                    output_dir=str(tmp_path))
        assert res["ok"] is True and res["count"] == 1

    def test_an_ssr_read_with_no_images_still_tries_the_browser(self, tmp_path, monkeypatch):
        """Rendered-but-empty is not an answer; it is a reason to look harder."""
        monkeypatch.setattr(engine, "read_post_http", lambda url, **kw: _http_meta(0))
        monkeypatch.setattr(browser_fetch, "fetch_post", _fake_post(_http_meta(2)))
        monkeypatch.setattr(engine, "_download_image",
                            lambda url, dest, **kw: dest.write_bytes(b"img"))

        res = engine.download_photo("https://www.tiktok.com/@photog/photo/77",
                                    output_dir=str(tmp_path))
        assert res["count"] == 2


class TestBatchPrePass:
    def test_a_batch_http_can_read_opens_no_browser_at_all(self, tmp_path, monkeypatch):
        monkeypatch.setattr(engine, "read_post_http", lambda url, **kw: _http_meta(1))

        async def _never(*a, **kw):
            pytest.fail("no browser should be launched")

        monkeypatch.setattr(browser_fetch, "_make_controller", _never)
        monkeypatch.setattr(engine, "_download_image",
                            lambda url, dest, **kw: dest.write_bytes(b"x"))

        urls = [f"https://www.tiktok.com/@u/photo/{i}" for i in (1, 2, 3)]
        res = engine.download_photos(urls, output_dir=str(tmp_path))
        assert res["count"] == 3 and res["images"] == 3

    def test_http_successes_survive_a_missing_browser(self, tmp_path, monkeypatch):
        """Work that already succeeded must not be discarded because the FALLBACK
        for the others is unavailable."""
        def _read(url, **kw):
            return _http_meta(1) if url.endswith("/1") else None

        monkeypatch.setattr(engine, "read_post_http", _read)

        async def _boom(headless, proxy, controller):
            raise browser_fetch.TikTokBrowserUnavailable("no patchright")

        monkeypatch.setattr(browser_fetch, "_make_controller", _boom)
        monkeypatch.setattr(engine, "_download_image",
                            lambda url, dest, **kw: dest.write_bytes(b"x"))

        urls = [f"https://www.tiktok.com/@u/photo/{i}" for i in (1, 2)]
        res = engine.download_photos(urls, output_dir=str(tmp_path))
        assert res["ok"] is True, "one post WAS saved"
        assert res["count"] == 1 and res["images"] == 1
        failed = [p for p in res["posts"] if p["url"].endswith("/2")][0]
        assert failed["ok"] is False and "patchright" in failed["error"]

    def test_only_the_unreadable_ones_reach_the_browser(self, tmp_path, monkeypatch):
        seen: list[str] = []

        def _read(url, **kw):
            return None if url.endswith("/2") else _http_meta(1)

        monkeypatch.setattr(engine, "read_post_http", _read)

        class FakeCtl:
            async def start(self):
                pass

            async def stop(self):
                pass

            @property
            def page(self):
                return None

        async def fake_make(headless, proxy, controller):
            return FakeCtl(), True, "browser"

        async def fake_fetch(url, **kw):
            seen.append(url)
            return {"meta": _http_meta(1, url=url), "comments": [], "captured": 1,
                    "engine": "browser", "signed_request": None}

        monkeypatch.setattr(browser_fetch, "_make_controller", fake_make)
        monkeypatch.setattr(browser_fetch, "fetch_post", fake_fetch)
        monkeypatch.setattr(engine, "_download_image",
                            lambda url, dest, **kw: dest.write_bytes(b"x"))

        urls = [f"https://www.tiktok.com/@u/photo/{i}" for i in (1, 2, 3)]
        res = engine.download_photos(urls, output_dir=str(tmp_path))
        assert [u.rsplit("/", 1)[-1] for u in seen] == ["2"], seen
        assert res["count"] == 3
