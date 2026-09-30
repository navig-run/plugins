"""The browser video tier: source selection, escalation, and teardown.

The teardown tests are the load-bearing ones. A leaked browser is silent — it
costs a window and ~120 MB of profile and reports nothing — so every error path
is asserted to stop the controller, not just the happy path.
"""

from __future__ import annotations

import json

import pytest

from navig_download.tiktok import browser_video
from navig_download.tiktok.browser_video import _video_struct, fetch_video, pick_source


# ── source selection ──────────────────────────────────────────────────────────

def test_pick_source_prefers_the_clean_stream():
    assert pick_source({"playAddr": "https://cdn/play", "downloadAddr": "https://cdn/wm"}) == \
        "https://cdn/play"


def test_watermark_asks_for_the_watermarked_render():
    got = pick_source({"playAddr": "https://cdn/play", "downloadAddr": "https://cdn/wm"},
                      watermark=True)
    assert got == "https://cdn/wm"


def test_empty_string_fields_are_not_a_url():
    """TikTok sends '' rather than omitting the key — that is not a source."""
    assert pick_source({"playAddr": "", "downloadAddr": ""}) is None


def test_falls_back_to_the_bitrate_ladder():
    video = {"playAddr": "", "bitrateInfo": [
        {"PlayAddr": {"UrlList": ["https://cdn/ladder.mp4"]}},
    ]}
    assert pick_source(video) == "https://cdn/ladder.mp4"


def test_no_video_struct_yields_none():
    assert pick_source({}) is None


def test_video_struct_found_in_the_detail_scope():
    scope = {
        "webapp.app-context": {"nope": 1},
        "webapp.video-detail": {"itemInfo": {"itemStruct": {
            "id": "42", "video": {"playAddr": "https://cdn/x"}}}},
    }
    assert _video_struct(scope)["id"] == "42"


def test_video_struct_ignores_a_detail_scope_with_no_video():
    """The page shell TikTok serves a gated post has the scope but no item."""
    scope = {"webapp.video-detail": {"itemInfo": {"itemStruct": {}}}}
    assert _video_struct(scope) == {}


# ── the browser tier itself ───────────────────────────────────────────────────

class _Resp:
    def __init__(self, body=b"video-bytes", ok=True, status=200, headers=None):
        self.ok, self.status = ok, status
        self._body, self.headers = body, headers or {}

    async def body(self):
        return self._body


class _Request:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    async def get(self, url, **kw):
        self.calls.append((url, kw))
        return self._resp


class _Page:
    def __init__(self, scope):
        self._scope = scope

    async def goto(self, *a, **kw):
        return None

    async def wait_for_timeout(self, *a):
        return None

    async def evaluate(self, _js):
        return json.dumps({"__DEFAULT_SCOPE__": self._scope})


class _Controller:
    """Records whether it was torn down — the whole point of these tests."""

    def __init__(self, scope, resp=None):
        self.page = _Page(scope)
        self.context = type("Ctx", (), {"request": _Request(resp or _Resp())})()
        self.started = self.stopped = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


_SCOPE = {"webapp.video-detail": {"itemInfo": {"itemStruct": {
    "id": "999", "video": {"playAddr": "https://cdn/play.mp4"}}}}}


@pytest.fixture
def wired(monkeypatch):
    """Install a fake controller factory; hand the test the live instance."""
    made = {}

    def _install(scope=_SCOPE, resp=None):
        ctrl = _Controller(scope, resp)
        made["ctrl"] = ctrl

        async def _make(headless, proxy, controller):
            return ctrl, True, "fake"

        async def _restore(_c, _host):
            return True

        monkeypatch.setattr("navig_download.tiktok.browser_fetch._make_controller", _make)
        monkeypatch.setattr("navig_download.tiktok.browser_fetch._restore_session", _restore)
        return ctrl

    _install.made = made
    return _install


async def test_downloads_through_the_browser_context(wired, tmp_path):
    ctrl = wired()
    path = await fetch_video("https://vm.tiktok.com/X", dest_dir=str(tmp_path))

    assert path.endswith("999.mp4")
    from pathlib import Path
    assert Path(path).read_bytes() == b"video-bytes"
    assert ctrl.started and ctrl.stopped


async def test_the_cdn_fetch_carries_a_tiktok_referer(wired, tmp_path):
    """The signed URL is refused without it — a silent 403 if this regresses."""
    ctrl = wired()
    await fetch_video("https://vm.tiktok.com/X", dest_dir=str(tmp_path))

    _url, kw = ctrl.context.request.calls[0]
    assert "tiktok.com" in kw["headers"]["Referer"]


async def test_a_gated_shell_is_reported_not_written(wired, tmp_path):
    from navig_download.tiktok.engine import TikTokBlocked

    ctrl = wired(scope={"webapp.video-detail": {"itemInfo": {"itemStruct": {}}}})
    with pytest.raises(TikTokBlocked):
        await fetch_video("https://vm.tiktok.com/X", dest_dir=str(tmp_path))
    assert ctrl.stopped, "the browser must be torn down on the no-data path"
    assert not list(tmp_path.iterdir()), "nothing may be written when there is no video"


async def test_the_failure_does_not_claim_a_login_that_never_happened(wired, tmp_path,
                                                                     monkeypatch):
    """No session restored → the message must not say 'logged-in browser'."""
    from navig_download.tiktok.engine import TikTokBlocked

    wired(scope={"webapp.video-detail": {"itemInfo": {"itemStruct": {}}}})

    async def _no_session(_c, _host):
        return False  # a vault with nothing saved for this host

    monkeypatch.setattr("navig_download.tiktok.browser_fetch._restore_session",
                        _no_session)
    with pytest.raises(TikTokBlocked) as err:
        await fetch_video("https://vm.tiktok.com/X", dest_dir=str(tmp_path))
    assert "logged-in browser" not in str(err.value)
    assert "navig tt login" in str(err.value)


async def test_a_restored_session_is_reported_as_such(wired, tmp_path):
    """Session restored and still refused → do not send the operator to login."""
    from navig_download.tiktok.engine import TikTokBlocked

    wired(scope={"webapp.video-detail": {"itemInfo": {"itemStruct": {}}}})
    with pytest.raises(TikTokBlocked) as err:
        await fetch_video("https://vm.tiktok.com/X", dest_dir=str(tmp_path))
    assert "logged-in browser" in str(err.value)
    assert "navig tt login" not in str(err.value)


async def test_an_empty_body_is_not_a_successful_download(wired, tmp_path):
    from navig_download.tiktok.engine import TikTokBlocked

    ctrl = wired(resp=_Resp(body=b""))
    with pytest.raises(TikTokBlocked):
        await fetch_video("https://vm.tiktok.com/X", dest_dir=str(tmp_path))
    assert ctrl.stopped
    assert not list(tmp_path.iterdir())


async def test_a_refusing_cdn_raises(wired, tmp_path):
    from navig_download.tiktok.engine import TikTokBlocked

    ctrl = wired(resp=_Resp(ok=False, status=403))
    with pytest.raises(TikTokBlocked, match="403"):
        await fetch_video("https://vm.tiktok.com/X", dest_dir=str(tmp_path))
    assert ctrl.stopped


def test_the_ceiling_respects_the_daemon_memory_budget():
    """The body arrives as ONE buffer, so the ceiling IS the peak allocation.

    core/CLAUDE.md budgets the daemon at 150 MB RSS under normal load. A ceiling
    above that is not a limit, it is permission to blow the budget — so this
    pins the relationship rather than the number, and still leaves room above
    Telegram's 50 MB bot-upload cap so nothing deliverable is excluded.
    """
    daemon_budget = 150 * 1024 * 1024
    telegram_cap = 50 * 1024 * 1024
    assert browser_video.MAX_BYTES < daemon_budget
    assert browser_video.MAX_BYTES > telegram_cap


async def test_an_oversized_video_is_refused_before_it_is_read(wired, tmp_path):
    """Declared too big → never pulled into memory."""
    from navig_download.tiktok.engine import TikTokBlocked

    ctrl = wired(resp=_Resp(headers={"content-length": str(500 * 1024 * 1024)}))
    with pytest.raises(TikTokBlocked, match="limit"):
        await fetch_video("https://vm.tiktok.com/X", dest_dir=str(tmp_path))
    assert ctrl.stopped


async def test_an_undeclared_oversized_body_is_still_refused(wired, tmp_path):
    """No content-length header — the size check must still happen after read."""
    from navig_download.tiktok.engine import TikTokBlocked

    wired(resp=_Resp(body=b"x" * 2048))
    with pytest.raises(TikTokBlocked, match="limit"):
        await fetch_video("https://vm.tiktok.com/X", dest_dir=str(tmp_path),
                          max_bytes=1024)
    assert not list(tmp_path.iterdir())


async def test_the_browser_is_torn_down_when_the_page_explodes(wired, tmp_path, monkeypatch):
    ctrl = wired()

    async def _boom(*a, **kw):
        raise RuntimeError("navigation died")

    monkeypatch.setattr(ctrl.page, "goto", _boom)
    with pytest.raises(RuntimeError):
        await fetch_video("https://vm.tiktok.com/X", dest_dir=str(tmp_path))
    assert ctrl.stopped, "a crash mid-navigation must not leak the browser"


async def test_a_caller_supplied_controller_is_not_stopped(wired, tmp_path, monkeypatch):
    """Whoever owns the browser closes it — `_download_photos_browser`'s rule."""
    ctrl = _Controller(_SCOPE)

    async def _make(headless, proxy, controller):
        return controller, False, "borrowed"

    async def _restore(_c, _host):
        return True

    monkeypatch.setattr("navig_download.tiktok.browser_fetch._make_controller", _make)
    monkeypatch.setattr("navig_download.tiktok.browser_fetch._restore_session", _restore)

    await fetch_video("https://vm.tiktok.com/X", dest_dir=str(tmp_path), controller=ctrl)
    assert not ctrl.stopped


async def test_no_session_host_skips_the_restore(wired, tmp_path, monkeypatch):
    """`--anon` must not quietly attach the operator's login."""
    wired()
    seen = []

    async def _restore(_c, host):
        seen.append(host)
        return True

    monkeypatch.setattr("navig_download.tiktok.browser_fetch._restore_session", _restore)
    await fetch_video("https://vm.tiktok.com/X", dest_dir=str(tmp_path), session_host=None)
    assert seen == []


# ── escalation from fetch_file ────────────────────────────────────────────────

@pytest.fixture
def refusing_ytdlp(monkeypatch):
    """Make yt-dlp fail the way a gated post makes it fail."""
    from navig_download.tiktok import engine

    class _YDL:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, *a, **kw):
            raise RuntimeError("Unable to extract webpage video data")

    mod = type("M", (), {"YoutubeDL": _YDL})
    monkeypatch.setitem(__import__("sys").modules, "yt_dlp", mod)
    monkeypatch.setattr(engine, "ytdlp_available", lambda: True, raising=False)
    return engine


def test_fetch_file_escalates_to_the_browser(refusing_ytdlp, monkeypatch, tmp_path):
    calls = []

    def _fake(url, **kw):
        calls.append(kw)
        out = tmp_path / "rescued.mp4"
        out.write_bytes(b"ok")
        return str(out)

    monkeypatch.setattr(browser_video, "fetch_video_sync", _fake)
    got = refusing_ytdlp.fetch_file("https://vm.tiktok.com/X", dest_dir=str(tmp_path))

    assert got.endswith("rescued.mp4")
    assert calls, "the browser tier was never reached"


def test_audio_only_escalates_and_yields_a_real_track(refusing_ytdlp, monkeypatch,
                                                      tmp_path):
    """Transcript and Audio must reach the same posts Video does."""
    out = tmp_path / "999.m4a"

    def _fake(url, **kw):
        out.write_bytes(b"aac")
        return str(out)

    monkeypatch.setattr(browser_video, "fetch_audio_sync", _fake)
    got = refusing_ytdlp.fetch_file("https://vm.tiktok.com/X", dest_dir=str(tmp_path),
                                    audio_only=True)
    assert got.endswith(".m4a")


def test_audio_only_never_returns_a_video_container(refusing_ytdlp, monkeypatch,
                                                    tmp_path):
    """No ffmpeg → decline. Telegram rejects an mp4 sent as an audio message."""
    monkeypatch.setattr(browser_video, "fetch_audio_sync", lambda *a, **k: None)
    video_calls = []
    monkeypatch.setattr(browser_video, "fetch_video_sync",
                        lambda *a, **k: video_calls.append(1))
    with pytest.raises(Exception):  # noqa: B017 — the original yt-dlp error
        refusing_ytdlp.fetch_file("https://vm.tiktok.com/X", dest_dir=str(tmp_path),
                                  audio_only=True)
    assert not video_calls, "audio must not fall back to handing back the video"


def test_no_ffmpeg_skips_the_browser_entirely(monkeypatch, tmp_path):
    """A browser we cannot use must not be launched — a `which` call is the cost."""
    launched = []
    monkeypatch.setattr(browser_video, "ffmpeg_path", lambda: None)
    monkeypatch.setattr(browser_video, "fetch_video_sync",
                        lambda *a, **k: launched.append(1))
    assert browser_video.fetch_audio_sync("https://vm.tiktok.com/X",
                                          dest_dir=str(tmp_path)) is None
    assert not launched


def test_the_video_is_deleted_once_the_track_is_out(monkeypatch, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"mp4")
    audio = tmp_path / "v.m4a"

    monkeypatch.setattr(browser_video, "ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(browser_video, "fetch_video_sync", lambda *a, **k: str(video))
    monkeypatch.setattr(browser_video, "extract_audio",
                        lambda p: (audio.write_bytes(b"aac"), str(audio))[1])

    assert browser_video.fetch_audio_sync("https://x", dest_dir=str(tmp_path))
    assert not video.exists(), "the pixels must not outlive an audio-only request"


def test_the_video_is_deleted_even_when_extraction_fails(monkeypatch, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"mp4")
    monkeypatch.setattr(browser_video, "ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(browser_video, "fetch_video_sync", lambda *a, **k: str(video))
    monkeypatch.setattr(browser_video, "extract_audio", lambda p: None)

    assert browser_video.fetch_audio_sync("https://x", dest_dir=str(tmp_path)) is None
    assert not video.exists()


# ── ffmpeg extraction ─────────────────────────────────────────────────────────

def test_extraction_declines_without_ffmpeg(monkeypatch, tmp_path):
    monkeypatch.setattr(browser_video, "ffmpeg_path", lambda: None)
    assert browser_video.extract_audio(str(tmp_path / "v.mp4")) is None


def test_a_zero_byte_output_is_not_a_track(monkeypatch, tmp_path):
    """ffmpeg exits 0 having written nothing — trusting the status would lie."""
    src = tmp_path / "v.mp4"
    src.write_bytes(b"mp4")
    monkeypatch.setattr(browser_video, "ffmpeg_path", lambda: "ffmpeg")

    def _run(cmd, **kw):
        (tmp_path / "v.m4a").write_bytes(b"")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr("subprocess.run", _run)
    assert browser_video.extract_audio(str(src)) is None


def test_stream_copy_is_tried_before_re_encoding(monkeypatch, tmp_path):
    src = tmp_path / "v.mp4"
    src.write_bytes(b"mp4")
    monkeypatch.setattr(browser_video, "ffmpeg_path", lambda: "ffmpeg")
    seen = []

    def _run(cmd, **kw):
        seen.append(cmd)
        (tmp_path / "v.m4a").write_bytes(b"aac")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr("subprocess.run", _run)
    assert browser_video.extract_audio(str(src))
    assert len(seen) == 1, "a successful copy must not also re-encode"
    assert "copy" in seen[0]


def test_a_failed_copy_falls_back_to_re_encoding(monkeypatch, tmp_path):
    src = tmp_path / "v.mp4"
    src.write_bytes(b"mp4")
    monkeypatch.setattr(browser_video, "ffmpeg_path", lambda: "ffmpeg")
    seen = []

    def _run(cmd, **kw):
        seen.append(cmd)
        if len(seen) > 1:  # the copy wrote nothing; the encode succeeds
            (tmp_path / "v.m4a").write_bytes(b"aac")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr("subprocess.run", _run)
    assert browser_video.extract_audio(str(src))
    assert len(seen) == 2 and "aac" in seen[1]


def test_the_batch_path_escalates_too(monkeypatch, tmp_path):
    """`navig tt download` was the one surface a login could not help."""
    # `downloader.main` resolves to the package's `main` FUNCTION, not the
    # module — import the module explicitly or the attribute lookup fails.
    import importlib

    dl = importlib.import_module("navig_download.downloader.main")

    landed = tmp_path / "creator" / "v.mp4"

    def _fake(url, **kw):
        landed.parent.mkdir(parents=True, exist_ok=True)
        landed.write_bytes(b"mp4")
        return str(landed)

    monkeypatch.setattr(browser_video, "fetch_video_sync", _fake)
    args = type("A", (), {"use_session": True})()
    assert dl._browser_rescue_batch("https://x", str(tmp_path / "creator"), False, args)


def test_the_batch_rescue_never_raises(monkeypatch, tmp_path):
    """One bad post must not abort a batch — the caller logs the real error."""
    # `downloader.main` resolves to the package's `main` FUNCTION, not the
    # module — import the module explicitly or the attribute lookup fails.
    import importlib

    dl = importlib.import_module("navig_download.downloader.main")

    def _boom(*a, **kw):
        raise RuntimeError("no browser")

    monkeypatch.setattr(browser_video, "fetch_video_sync", _boom)
    args = type("A", (), {"use_session": True})()
    assert dl._browser_rescue_batch("https://x", str(tmp_path), False, args) is False


def test_the_batch_rescue_is_skipped_with_no_destination(monkeypatch):
    """A URL that never parsed has nowhere to write — do not launch a browser."""
    # `downloader.main` resolves to the package's `main` FUNCTION, not the
    # module — import the module explicitly or the attribute lookup fails.
    import importlib

    dl = importlib.import_module("navig_download.downloader.main")

    launched = []
    monkeypatch.setattr(browser_video, "fetch_video_sync",
                        lambda *a, **k: launched.append(1))
    args = type("A", (), {"use_session": True})()
    assert dl._browser_rescue_batch("https://x", None, False, args) is False
    assert not launched


def test_the_batch_rescue_honours_anon(monkeypatch, tmp_path):
    # `downloader.main` resolves to the package's `main` FUNCTION, not the
    # module — import the module explicitly or the attribute lookup fails.
    import importlib

    dl = importlib.import_module("navig_download.downloader.main")

    seen = {}

    def _fake(url, **kw):
        seen.update(kw)
        p = tmp_path / "v.mp4"
        p.write_bytes(b"mp4")
        return str(p)

    monkeypatch.setattr(browser_video, "fetch_video_sync", _fake)
    args = type("A", (), {"use_session": False})()
    dl._browser_rescue_batch("https://x", str(tmp_path), False, args)
    assert seen["session_host"] is None


def test_concurrent_rescues_never_overlap(monkeypatch, tmp_path):
    """`--workers 10` on a gated batch must not launch ten browsers at once."""
    import importlib
    import threading
    from concurrent import futures

    dl = importlib.import_module("navig_download.downloader.main")
    live = {"now": 0, "peak": 0}
    guard = threading.Lock()

    def _fake(url, **kw):
        with guard:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        try:
            threading.Event().wait(0.05)  # hold the "browser" open
        finally:
            with guard:
                live["now"] -= 1
        p = tmp_path / f"{url[-1]}.mp4"
        p.write_bytes(b"mp4")
        return str(p)

    monkeypatch.setattr(browser_video, "fetch_video_sync", _fake)
    args = type("A", (), {"use_session": True})()
    with futures.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(
            lambda i: dl._browser_rescue_batch(f"https://x/{i}", str(tmp_path), False, args),
            range(8)))

    assert live["peak"] == 1, (
        f"{live['peak']} browsers ran concurrently — the rescue lock is not holding")


# ── the CLI escalates on a login gate instead of giving up ────────────────────

def test_analyse_escalates_a_login_gate_to_the_browser():
    """It used to `raise typer.Exit(2)` — failing on posts the browser can read.

    Asserted on the AST rather than by invoking the Typer command: driving that
    command means supplying its whole option surface, so the test would break on
    any unrelated flag change while saying nothing about this behaviour.
    """
    import ast
    import inspect

    from navig_download.commands import download as d

    fn = ast.parse(inspect.getsource(d.tiktok_analyse)).body[0]
    handler = next(
        h for node in ast.walk(fn) if isinstance(node, ast.Try)
        for h in node.handlers
        if h.type is not None and "TikTokLoginRequired" in ast.unparse(h.type)
    )
    body = ast.unparse(handler)
    assert "_brief_via_browser" in body, "the login branch must try the browser tier"
    assert "typer.Exit" not in body, "it must not give up on a post the browser can read"


def test_the_shared_escalation_renders_a_brief(monkeypatch):
    """Both branches route through one helper — it must actually produce output."""
    import asyncio

    from navig_download.commands import download as d
    from navig_download.tiktok import engine

    monkeypatch.setattr(d, "_browser_post",
                        lambda *a, **k: {"meta": {"id": "1"}, "comments": [{"t": "x"}]})
    monkeypatch.setattr(d, "_present_info", lambda m, as_json: True)
    monkeypatch.setattr(engine, "brief_meta", lambda m: asyncio.sleep(0, result="B"))
    seen = {}
    monkeypatch.setattr(d, "_render_analysis", lambda m, b: seen.update(meta=m, brief=b))

    d._brief_via_browser("https://x", comments=3, proxy=None, login=None)
    assert seen["brief"] == "B"
    assert seen["meta"]["comments"] == [{"t": "x"}], "comments must reach the briefing"


def test_an_unreadable_browser_read_renders_nothing(monkeypatch):
    """`_present_info` False means the message was already shown — do not brief."""
    from navig_download.commands import download as d

    monkeypatch.setattr(d, "_browser_post", lambda *a, **k: {"meta": {}, "comments": []})
    monkeypatch.setattr(d, "_present_info", lambda m, as_json: False)
    rendered = []
    monkeypatch.setattr(d, "_render_analysis", lambda m, b: rendered.append(1))

    d._brief_via_browser("https://x", comments=0, proxy=None, login=None)
    assert not rendered


# ── the batch path: share links, and an honest count ──────────────────────────

def _dl_main():
    import importlib

    return importlib.import_module("navig_download.downloader.main")


def test_a_share_link_is_resolved_before_it_is_parsed(monkeypatch, tmp_path):
    """vm.tiktok.com/XXXX has NO path — parsing it raw dies before any fetch.

    This is the link format the TikTok app produces, so the batch path failed on
    the most common input there is with "Username cannot be empty".
    """
    dl = _dl_main()
    canonical = "https://www.tiktok.com/@someone/video/123"
    monkeypatch.setattr("navig_download.tiktok.engine.resolve_url",
                        lambda u, **kw: canonical)
    seen = {}

    class _YDL:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, link, **kw):
            seen["link"] = link
            return {"title": "t", "id": "123"}

    monkeypatch.setattr(dl, "yt_dlp", type("M", (), {"YoutubeDL": _YDL}))
    args = type("A", (), {"output_dir": str(tmp_path), "skip_existing": False,
                          "save_metadata": False, "no_rate_limit": True,
                          "throttle_rate": None, "use_session": True})()

    assert dl.download_from_url("https://vm.tiktok.com/ZGdxAM5DG", False, args, 0, 0) is True
    assert seen["link"] == canonical
    assert (tmp_path / "someone").is_dir(), "the creator folder comes from the RESOLVED url"


def test_the_error_log_lands_beside_the_downloads_not_in_the_cwd(monkeypatch, tmp_path):
    """A bare "logs" path created a folder wherever the operator was standing."""
    dl = _dl_main()
    out = tmp_path / "out"
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr("navig_download.tiktok.engine.resolve_url",
                        lambda u, **kw: "https://www.tiktok.com/@who/video/1")

    class _YDL:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, *a, **kw): raise RuntimeError("nope")

    monkeypatch.setattr(dl, "yt_dlp", type("M", (), {"YoutubeDL": _YDL}))
    monkeypatch.setattr(dl, "_browser_rescue_batch", lambda *a, **k: False)
    args = type("A", (), {"output_dir": str(out), "skip_existing": False,
                          "save_metadata": False, "no_rate_limit": True,
                          "throttle_rate": None, "use_session": True})()

    assert dl.download_from_url("https://x/y/video/1", False, args, 0, 0) is False
    assert (out / "logs" / "errors.txt").exists(), "the log belongs with the batch"
    assert not (cwd / "logs").exists(), "nothing may be written into the cwd"


def test_the_batch_rescue_can_be_switched_off(monkeypatch, tmp_path):
    """browser_fallback=False must actually stop the launch, not just be read."""
    dl = _dl_main()
    launched = []
    monkeypatch.setattr(browser_video, "fetch_video_sync",
                        lambda *a, **k: launched.append(1))
    args = type("A", (), {"use_session": True, "browser_fallback": False})()

    assert dl._browser_rescue_batch("https://x", str(tmp_path), False, args) is False
    assert not launched


def test_download_urls_puts_browser_fallback_where_the_rescue_reads_it(monkeypatch,
                                                                      tmp_path):
    """The reader took it off the namespace; nothing ever wrote it there.

    `use_session` was threaded and this was not, so `getattr(args,
    "browser_fallback", True)` could only ever be True — a knob with a reader and
    no writer. This asserts the value actually arrives.
    """
    from navig_download.tiktok import engine

    seen = {}

    class _RK:
        @staticmethod
        def download_from_url(url, watermark, ns, *a):
            seen["fallback"] = getattr(ns, "browser_fallback", "ABSENT")
            seen["session"] = getattr(ns, "use_session", "ABSENT")
            return False

    monkeypatch.setattr(engine, "_downloader_main", lambda: _RK)
    engine.download_urls(["https://x/1"], output_dir=str(tmp_path),
                         browser_fallback=False, use_session=False)
    assert seen["fallback"] is False
    assert seen["session"] is False, "the existing knob must keep working too"


def test_download_urls_defaults_browser_fallback_on(monkeypatch, tmp_path):
    from navig_download.tiktok import engine

    seen = {}

    class _RK:
        @staticmethod
        def download_from_url(url, watermark, ns, *a):
            seen["fallback"] = getattr(ns, "browser_fallback", "ABSENT")
            return False

    monkeypatch.setattr(engine, "_downloader_main", lambda: _RK)
    engine.download_urls(["https://x/1"], output_dir=str(tmp_path))
    assert seen["fallback"] is True


def test_a_batch_where_everything_failed_does_not_report_success(monkeypatch, tmp_path):
    """It returned ok=True and count=len(urls) unconditionally. Measured: one
    share link, zero files, ok=True."""
    from navig_download.tiktok import engine

    monkeypatch.setattr(engine, "_downloader_main",
                        lambda: type("RK", (), {"download_from_url":
                                                staticmethod(lambda *a, **k: False)}))
    res = engine.download_urls(["https://vm.tiktok.com/A", "https://vm.tiktok.com/B"],
                               output_dir=str(tmp_path))
    assert res["ok"] is False
    assert res["count"] == 0
    assert res["attempted"] == 2, "attempted stays visible — 0 of 2 is the useful shape"


def test_a_partial_batch_counts_only_what_landed(monkeypatch, tmp_path):
    from navig_download.tiktok import engine

    results = iter([True, False, True])
    monkeypatch.setattr(engine, "_downloader_main",
                        lambda: type("RK", (), {"download_from_url":
                                                staticmethod(lambda *a, **k: next(results))}))
    res = engine.download_urls(["https://x/1", "https://x/2", "https://x/3"],
                               output_dir=str(tmp_path), workers=1)
    assert res["ok"] is True and res["count"] == 2 and res["attempted"] == 3


def test_extraction_is_bounded_by_a_timeout(monkeypatch, tmp_path):
    """An unbounded ffmpeg wedges the caller forever — a documented class here."""
    src = tmp_path / "v.mp4"
    src.write_bytes(b"mp4")
    monkeypatch.setattr(browser_video, "ffmpeg_path", lambda: "ffmpeg")
    seen = {}

    def _run(cmd, **kw):
        seen.update(kw)
        (tmp_path / "v.m4a").write_bytes(b"aac")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr("subprocess.run", _run)
    browser_video.extract_audio(str(src))
    assert seen.get("timeout"), "ffmpeg must never run unbounded"


def test_a_failed_rescue_surfaces_the_original_diagnosis(refusing_ytdlp, monkeypatch,
                                                         tmp_path):
    """The precise yt-dlp error must survive — not be replaced by 'browser failed'."""
    def _boom(*a, **kw):
        raise RuntimeError("camoufox is not installed")

    monkeypatch.setattr(browser_video, "fetch_video_sync", _boom)
    with pytest.raises(Exception) as err:
        refusing_ytdlp.fetch_file("https://vm.tiktok.com/X", dest_dir=str(tmp_path))
    assert "camoufox" not in str(err.value)


def test_browser_fallback_can_be_switched_off(refusing_ytdlp, monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(browser_video, "fetch_video_sync",
                        lambda *a, **k: called.append(1))
    with pytest.raises(Exception):  # noqa: B017
        refusing_ytdlp.fetch_file("https://vm.tiktok.com/X", dest_dir=str(tmp_path),
                                  browser_fallback=False)
    assert not called


def test_use_session_false_reaches_the_browser_anonymously(refusing_ytdlp, monkeypatch,
                                                           tmp_path):
    """`--anon` is honoured on the browser tier too, not only on the yt-dlp one."""
    seen = {}

    def _fake(url, **kw):
        seen.update(kw)
        out = tmp_path / "a.mp4"
        out.write_bytes(b"ok")
        return str(out)

    monkeypatch.setattr(browser_video, "fetch_video_sync", _fake)
    refusing_ytdlp.fetch_file("https://vm.tiktok.com/X", dest_dir=str(tmp_path),
                              use_session=False)
    assert seen["session_host"] is None
