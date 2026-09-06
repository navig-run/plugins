"""Download a post's video through a real browser, when yt-dlp cannot read it.

yt-dlp is the fast path and stays the default. But TikTok serves some posts only
to a **browser**, and the measurements say that precisely — the distinction
matters because it is easy to assume the wrong half is doing the work:

===========================  ============================================
yt-dlp, vaulted session      fails
yt-dlp, anonymous            fails — byte-identical to the above
plain HTTP SSR read          page **shell**: no uploader, no description,
                             no images
browser, vaulted session     full item struct, 292 KB blob, ``playAddr``
                             and ``downloadAddr`` present
browser, **no session**      identical — downloads the same 4.05 MB file
===========================  ============================================

So it is the browser that closes the gap, not the cookies. A session was never
observed to change the outcome on this post, and an earlier version of this
docstring implied it did. The session is still restored when one exists — a
genuinely private post is a class this could not test, and offering the session
costs nothing — but nothing here should be read as "log in and this works".

So this tier exists for exactly the case the other tiers cannot serve, and is
reached only after they fail. The design mirrors :func:`engine.download_photos`,
which already escalates the *photo* path to a browser for the same reason.

Two things the CDN requires, and why the bytes are fetched through the browser
context rather than with ``requests``:

* the signed ``playAddr`` is bound to the **session** that was served the page;
* it wants the TikTok ``Referer``.

``context.request`` shares the context's cookie jar, so the fetch is made with
the same identity that read the page — no cookie translation, nothing written to
disk, and the session never leaves the browser.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Refuse a video larger than this rather than pulling it into memory.
#:
#: ``context.request`` has no streaming API — the body arrives as ONE bytes
#: object — so this ceiling is also the peak allocation, and the daemon that
#: calls this documents a 150 MB RSS budget under normal load. A ceiling above
#: that budget is not a safety limit, it is a licence to blow it: the first
#: pathological post would double the daemon's memory before anything refused
#: it. 80 MB is 20x the measured size of a real gated post, comfortably past
#: Telegram's own 50 MB bot-upload cap (so nothing deliverable is excluded), and
#: leaves the budget intact.
#:
#: Callers that genuinely want more pass ``max_bytes`` — the CLI is not bound by
#: the daemon's budget. Streaming to disk would remove the ceiling entirely, but
#: it needs an HTTP client outside the browser context, and the whole reason the
#: bytes are fetched through that context is that the signed CDN URL is bound to
#: the session which served the page.
MAX_BYTES = 80 * 1024 * 1024

#: How long to let the page settle before reading the blob. The SSR data is in
#: the initial HTML, so this is slack for a redirect/interstitial, not a poll.
_SETTLE_MS = 3500

_BLOB_JS = """() => {
  const e = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
  return e ? e.textContent : '';
}"""


def _video_struct(scope: dict) -> dict:
    """The post's ``video`` dict out of a parsed ``__DEFAULT_SCOPE__``."""
    for key, val in (scope or {}).items():
        if not isinstance(val, dict) or "detail" not in key.lower():
            continue
        item = (val.get("itemInfo") or {}).get("itemStruct") or {}
        if isinstance(item, dict) and item.get("video"):
            return item
    return {}


def pick_source(video: dict, *, watermark: bool = False) -> str | None:
    """The best playable URL in a ``video`` struct, or None.

    ``downloadAddr`` is the watermarked render TikTok's own "save" button uses;
    ``playAddr`` is the clean stream. Requesting the watermark and getting the
    clean copy would be a quiet lie about what was downloaded, so *watermark*
    picks a real watermarked source or falls through — it never silently
    substitutes the other one.
    """
    order = ("downloadAddr", "playAddr") if watermark else ("playAddr", "downloadAddr")
    for field in order:
        url = video.get(field)
        if isinstance(url, str) and url.startswith("http"):
            return url
    # The bitrate ladder is the last resort: same URLs, nested one level deeper,
    # and present on some responses where the flat fields are empty strings.
    for entry in video.get("bitrateInfo") or []:
        urls = ((entry or {}).get("PlayAddr") or {}).get("UrlList") or []
        for url in urls:
            if isinstance(url, str) and url.startswith("http"):
                return url
    return None


async def _read_blob(page: Any) -> dict:
    import json  # noqa: PLC0415

    raw = await page.evaluate(_BLOB_JS)
    if not raw:
        return {}
    try:
        return (json.loads(raw) or {}).get("__DEFAULT_SCOPE__") or {}
    except (ValueError, TypeError) as exc:
        logger.debug("[browser_video] SSR blob unparseable: %s", exc)
        return {}


async def fetch_video(
    url: str,
    *,
    dest_dir: str,
    watermark: bool = False,
    headless: bool = True,
    session_host: str | None = "tiktok.com",
    proxy: str | None = None,
    controller: Any = None,
    max_bytes: int = MAX_BYTES,
) -> str:
    """Download *url*'s video with a real browser. Returns the file path.

    Raises rather than returning a partial result: a caller that gets a path
    back must be able to trust the bytes are there. The browser is always torn
    down, including on every error path — a leaked browser is silent, which is
    what makes it worth a ``finally``.
    """
    from .browser_fetch import TikTokBrowserUnavailable, _make_controller, _restore_session
    from .engine import TikTokBlocked, TikTokNoVideo, canonical_url

    ctrl, owns, _name = await _make_controller(headless, proxy, controller)
    try:
        if owns:
            await ctrl.start()
        # Keep the answer: `_restore_session` reports whether cookies actually
        # landed, and "no session was offered" and "a session was offered and
        # refused" need different words below. Discarding it would leave the
        # failure claiming a login that never happened.
        signed_in = bool(session_host) and await _restore_session(ctrl, session_host)

        page = ctrl.page
        await page.goto(canonical_url(url), timeout=60_000, wait_until="domcontentloaded")
        await page.wait_for_timeout(_SETTLE_MS)

        item = _video_struct(await _read_blob(page))
        if not item:
            # The browser is the last tier. If it cannot see the item data
            # either, say that plainly instead of implying a transient hiccup —
            # and do not claim a logged-in browser when there was no session.
            raise TikTokBlocked(
                "TikTok did not serve this post's data even to a logged-in browser"
                if signed_in else
                "TikTok did not serve this post's data, and the browser had no "
                "saved session to offer — sign in with `navig tt login`"
            )
        source = pick_source(item.get("video") or {}, watermark=watermark)
        if not source:
            raise TikTokNoVideo("this TikTok post carries no downloadable video track")

        resp = await ctrl.context.request.get(
            source, headers={"Referer": "https://www.tiktok.com/"}, timeout=120_000
        )
        if not resp.ok:
            raise TikTokBlocked(f"the video CDN refused the download (HTTP {resp.status})")
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise TikTokBlocked(
                f"video is {int(declared) // (1024 * 1024)} MB — over the "
                f"{max_bytes // (1024 * 1024)} MB download limit"
            )
        body = await resp.body()
        if not body:
            raise TikTokBlocked("the video CDN returned an empty response")
        if len(body) > max_bytes:
            raise TikTokBlocked(
                f"video is {len(body) // (1024 * 1024)} MB — over the "
                f"{max_bytes // (1024 * 1024)} MB download limit"
            )

        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / f"{item.get('id') or 'tiktok'}.mp4"
        path.write_bytes(body)
        logger.info("[browser_video] saved %.1f MB via the browser tier",
                    len(body) / (1024 * 1024))
        return str(path)
    except TikTokBrowserUnavailable:
        raise
    finally:
        if owns:
            try:
                await ctrl.stop()
            except Exception:  # noqa: BLE001 — teardown must not mask the real error
                logger.debug("[browser_video] controller stop failed", exc_info=True)


def fetch_video_sync(url: str, **kw: Any) -> str:
    """Blocking wrapper — the shape :func:`engine.fetch_file` can call."""
    import asyncio  # noqa: PLC0415

    return asyncio.run(fetch_video(url, **kw))


# ── audio, for the posts only this tier can reach ─────────────────────────────

#: Stream-copy first: TikTok ships AAC in MP4, so the track lifts out with no
#: re-encode — instant, and bit-identical to what the post actually plays.
_COPY_ARGS = ("-vn", "-acodec", "copy")
#: Some posts carry a codec the .m4a container will not hold. Re-encoding is
#: slower and lossy, but a slightly-degraded track the operator can hear beats
#: reporting failure on a post the browser tier already successfully fetched.
_ENCODE_ARGS = ("-vn", "-acodec", "aac", "-b:a", "128k")

#: An unbounded ffmpeg is how a wedged child hangs the caller forever. A minute
#: is generous for a stream copy of a clip that is capped at MAX_BYTES anyway.
_FFMPEG_TIMEOUT = 60


def ffmpeg_path() -> str | None:
    import shutil  # noqa: PLC0415

    return shutil.which("ffmpeg")


def extract_audio(video_path: str) -> str | None:
    """Lift the audio track out of *video_path*. Path to the .m4a, or None.

    None whenever the track cannot be produced — no ffmpeg, or a file with no
    audio at all. The caller degrades to the original error rather than handing
    back a video renamed to .m4a, which Telegram would reject as an audio
    message and a transcriber would waste a download on.
    """
    import subprocess  # noqa: PLC0415

    exe = ffmpeg_path()
    if not exe:
        logger.debug("[browser_video] ffmpeg not installed — cannot extract audio")
        return None
    src = Path(video_path)
    out = src.with_suffix(".m4a")
    for args in (_COPY_ARGS, _ENCODE_ARGS):
        try:
            subprocess.run([exe, "-y", "-i", str(src), *args, str(out)],
                           capture_output=True, timeout=_FFMPEG_TIMEOUT, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("[browser_video] ffmpeg failed: %s", exc)
            return None
        # Trust the file, not the exit status: ffmpeg returns 0 in cases where it
        # wrote nothing usable, and a zero-byte .m4a presented as an audio track
        # is the phantom-success shape this whole module exists to avoid.
        if out.exists() and out.stat().st_size > 0:
            return str(out)
    logger.debug("[browser_video] no audio track in %s", src.name)
    return None


def fetch_audio_sync(url: str, **kw: Any) -> str | None:
    """Browser-download the video, then hand back only its audio track.

    The video is deleted once the track is out: the caller asked for audio, and
    leaving the pixels behind would double the footprint of every transcription
    of a post that took this route.
    """
    if not ffmpeg_path():
        return None  # cheap check first — do not launch a browser we cannot use
    video = fetch_video_sync(url, **kw)
    try:
        return extract_audio(video)
    finally:
        try:
            Path(video).unlink(missing_ok=True)
        except OSError:  # noqa: S110 — a stray temp file is not worth failing over
            pass


__all__ = ["MAX_BYTES", "extract_audio", "fetch_audio_sync", "fetch_video",
           "fetch_video_sync", "ffmpeg_path", "pick_source"]
