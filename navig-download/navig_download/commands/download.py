"""NAVIG TikTok CLI — the full download engine + AI briefings, wired natively.

The download engine (`navig_download.downloader`) is bundled with
navig-download and runs **in-process** — every capability is a
`navig tiktok` subcommand; there is no separate tool to install or run.

  navig tiktok download <url…>    organized archival download (multi-URL, concurrent)
  navig tiktok batch <file>       download every URL listed in a text file
  navig tiktok profile  <user>    download a whole profile (filters, archive, limits)
  navig tiktok info     <url>     metadata: creator · country · description · stats
  navig tiktok comments <url>     top comments ranked by likes
  navig tiktok analyse  <url>     AI markdown briefing (description + top comments)
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Optional

import typer

from navig.lazy_loader import lazy_import

ch = lazy_import("navig.console_helper")

tiktok_app = typer.Typer(
    name="tiktok",
    help="🎵 TikTok — download (videos/profiles/images), metadata + AI briefings",
    no_args_is_help=True,
)

# friendly CLI filter names → the engine's --content-type vocabulary
_CONTENT_MAP = {
    "all": "all",
    "videos": "video-only",
    "audio": "audio-only",
    "images": "images-only",
    "metadata": "metadata-only",
}


def _require_downloader() -> bool:
    from navig_download.tiktok import downloader_available

    if downloader_available():
        return True
    ch.error(
        "the TikTok download engine is missing — reinstall the plugin:\n"
        "  navig store install pip:navig-download"
    )
    return False


def _require_ytdlp() -> bool:
    from navig_download.tiktok import ytdlp_available

    if ytdlp_available():
        return True
    ch.error("yt-dlp is missing (a dependency of navig-download).\n  pip install yt-dlp")
    return False


def _fmt_stats(meta: dict) -> str:
    bits = []
    for label, key in (("views", "view_count"), ("likes", "like_count"),
                       ("comments", "comment_count"), ("shares", "repost_count")):
        if meta.get(key) is not None:
            bits.append(f"{label} {meta[key]:,}")
    return " · ".join(bits)


def _download_mixed(urls: list[str], *, out: str, watermark: bool, workers: int,
                    skip_existing: bool, metadata: bool, delay: float, throttle: str,
                    no_rate_limit: bool, login: Optional[bool] = None) -> None:
    """Download a mix of video + /photo/ URLs: videos via yt-dlp, photos via the post reader.

    TikTok photo carousels (``/photo/…``) aren't supported by yt-dlp, so they route to the
    slideshow reader (image URLs are signed + short-lived → fetched immediately).

    The split MUST resolve share links (``is_photo_post``, not ``is_photo_url``): a
    ``vm.tiktok.com`` link has no path, so classifying on the raw string sent every
    shared photo post down the video path, where yt-dlp cannot read it at all.
    """
    from navig_download.tiktok import engine

    photos = [u for u in urls if engine.is_photo_post(u)]
    videos = [u for u in urls if not engine.is_photo_post(u)]

    if videos:
        # The vaulted session now reaches the yt-dlp path too, so `--anon` has to be
        # honoured HERE as well — it was previously consulted only for photos,
        # because videos had no session to opt out of.
        vres = engine.download_urls(
            videos, output_dir=out, watermark=watermark, workers=workers,
            skip_existing=skip_existing, save_metadata=metadata, delay=delay,
            throttle_rate=throttle or None, no_rate_limit=no_rate_limit,
            use_session=login is not False,
        )
        if vres["count"] < len(videos):
            ch.warning(f"{len(videos) - vres['count']} video arg(s) skipped (not http(s) URLs).")
        ch.success(f"Videos: attempted {vres['count']} → {vres['output_dir']}")
        ch.dim("Any per-video failures are logged to errors.txt in the output dir.")

    if photos:
        reuse = " (reusing one session)" if len(photos) > 1 else ""
        ch.info(f"{len(photos)} photo post(s) → stealth browser{reuse} — yt-dlp can't read these …")
        session_host = _resolve_session(login)  # auto-restore a saved session for gated posts
        pres = engine.download_photos(photos, output_dir=out, save_metadata=metadata,
                                      session_host=session_host)
        if pres.get("error") and not pres.get("posts"):
            # Nothing downloaded at all. Falling through printed
            # "Photos: 0/N post(s), 0 image(s) saved." in GREEN immediately under the
            # error, and exited 0 — so a total failure read as a successful run.
            ch.error(pres["error"])
            raise typer.Exit(1)
        for pr in pres.get("posts", []):
            if pr.get("ok"):
                ch.success(f"  ✓ {pr['count']}/{pr['total']} images → {pr['output_dir']}")
            else:
                ch.warning(f"  ✗ {pr.get('url', '?')}: {pr.get('error', 'failed')}")
        ch.success(f"Photos: {pres.get('count', 0)}/{len(photos)} post(s), "
                   f"{pres.get('images', 0)} image(s) saved.")


# ── download (bundled engine, in-process) ────────────────────────────────────────────

@tiktok_app.command("download")
def tiktok_download(
    urls: Annotated[list[str], typer.Argument(help="One or more TikTok video/photo URLs")],
    out: Annotated[str, typer.Option("--out", "-o", help="Output directory")] = "downloads",
    watermark: Annotated[bool, typer.Option("--watermark", help="Keep the watermark")] = False,
    workers: Annotated[int, typer.Option("--workers", "-w", help="Concurrent downloads (2-5 recommended)")] = 2,
    skip_existing: Annotated[bool, typer.Option("--skip-existing", help="Skip files already downloaded")] = False,
    metadata: Annotated[bool, typer.Option("--metadata/--no-metadata", help="Save per-post metadata JSON")] = True,
    delay: Annotated[float, typer.Option("--delay", help="Base seconds between downloads (±50% jitter)")] = 2.0,
    throttle: Annotated[str, typer.Option("--throttle", help="Limit speed, e.g. 500K / 1M")] = "",
    no_rate_limit: Annotated[bool, typer.Option("--no-rate-limit", help="Disable rate limiting (IP-ban risk)")] = False,
    login: _LoginOpt = None,
) -> None:
    """Download TikTok posts (organized <out>/<creator>/<id>) — concurrent, resumable.

    Handles both videos (yt-dlp) and **photo carousels** (`/photo/…`, via the browser —
    yt-dlp can't); mixed lists are routed automatically.
    """
    if not _require_downloader():
        raise typer.Exit(1)
    if no_rate_limit:
        ch.warning("Rate limiting disabled — high risk of TikTok blocking your IP.")
    ch.info(f"Downloading {len(urls)} URL(s) → {out}/ …")
    _download_mixed(urls, out=out, watermark=watermark, workers=workers,
                    skip_existing=skip_existing, metadata=metadata, delay=delay,
                    throttle=throttle, no_rate_limit=no_rate_limit, login=login)


@tiktok_app.command("batch")
def tiktok_batch(
    links_file: Annotated[Path, typer.Argument(help="Text file with one TikTok URL per line")],
    out: Annotated[str, typer.Option("--out", "-o", help="Output directory")] = "downloads",
    watermark: Annotated[bool, typer.Option("--watermark", help="Keep the watermark")] = False,
    workers: Annotated[int, typer.Option("--workers", "-w", help="Concurrent downloads (2-5 recommended)")] = 2,
    skip_existing: Annotated[bool, typer.Option("--skip-existing", help="Skip files already downloaded")] = False,
    metadata: Annotated[bool, typer.Option("--metadata/--no-metadata", help="Save per-post metadata JSON")] = True,
    delay: Annotated[float, typer.Option("--delay", help="Base seconds between downloads (±50% jitter)")] = 2.0,
    throttle: Annotated[str, typer.Option("--throttle", help="Limit speed, e.g. 500K / 1M")] = "",
    no_rate_limit: Annotated[bool, typer.Option("--no-rate-limit", help="Disable rate limiting (IP-ban risk)")] = False,
    login: _LoginOpt = None,
) -> None:
    """Download every URL in a links file (videos via yt-dlp, /photo/ posts via the browser)."""
    if not _require_downloader():
        raise typer.Exit(1)
    if not links_file.is_file():
        ch.error(f"Links file not found: {links_file}")
        raise typer.Exit(1)
    # utf-8-sig: tolerate a BOM from Windows editors (else the first URL breaks)
    urls = [ln.strip() for ln in links_file.read_text(encoding="utf-8-sig").splitlines() if ln.strip()]
    if not urls:
        ch.warning(f"No URLs found in {links_file}.")
        raise typer.Exit(0)
    if no_rate_limit:
        ch.warning("Rate limiting disabled — high risk of TikTok blocking your IP.")
    if workers > 5:
        ch.warning(">5 workers increases the risk of IP blocking; 2-5 recommended.")
    ch.info(f"Batch: {len(urls)} URL(s) from {links_file} → {out}/ …")
    _download_mixed(urls, out=out, watermark=watermark, workers=workers,
                    skip_existing=skip_existing, metadata=metadata, delay=delay,
                    throttle=throttle, no_rate_limit=no_rate_limit, login=login)


@tiktok_app.command("profile")
def tiktok_profile(
    username: Annotated[str, typer.Argument(help="TikTok @username (no @ needed)")],
    out: Annotated[str, typer.Option("--out", "-o", help="Output directory")] = "downloads",
    max_downloads: Annotated[int, typer.Option("--max", help="Max posts to fetch (0 = all)")] = 0,
    content: Annotated[str, typer.Option("--content", "-c",
        help="What to fetch: all | videos | audio | images | metadata")] = "videos",
    watermark: Annotated[bool, typer.Option("--watermark", help="Keep the watermark")] = False,
    archive: Annotated[bool, typer.Option("--archive/--no-archive",
        help="Track downloads in archive.txt (skip re-downloads across runs)")] = True,
    skip_existing: Annotated[bool, typer.Option("--skip-existing", help="Skip files already on disk")] = False,
    delay: Annotated[float, typer.Option("--delay", help="Base seconds between downloads (±50% jitter)")] = 2.0,
    throttle: Annotated[str, typer.Option("--throttle", help="Limit speed, e.g. 500K / 1M")] = "",
    no_rate_limit: Annotated[bool, typer.Option("--no-rate-limit", help="Disable rate limiting (IP-ban risk)")] = False,
) -> None:
    """Download a creator's whole profile — videos, slideshows images, audio, or just metadata."""
    if not _require_downloader():
        raise typer.Exit(1)
    content_type = _CONTENT_MAP.get(content.lower())
    if content_type is None:
        ch.error(f"Unknown --content '{content}'.", "Use: all | videos | audio | images | metadata")
        raise typer.Exit(1)
    from navig_download.tiktok import engine

    if no_rate_limit:
        ch.warning("Rate limiting disabled — high risk of TikTok blocking your IP.")
    user = username.lstrip("@")
    res = engine.download_profile(
        user, output_dir=out, max_downloads=max_downloads or None,
        use_archive=archive, watermark=watermark, content_type=content_type,
        skip_existing=skip_existing, delay=delay,
        throttle_rate=throttle or None, no_rate_limit=no_rate_limit,
    )
    if not res.get("success"):
        ch.error(res.get("error") or "profile download failed")
        raise typer.Exit(1)


# ── metadata / analysis (yt-dlp) ──────────────────────────────────────────────

# Shared anti-detection options exposed on every metadata command. Proxy/cookies also
# default from env (NAVIG_TIKTOK_PROXY / NAVIG_TIKTOK_COOKIES[_FROM_BROWSER]).
_ProxyOpt = Annotated[Optional[str], typer.Option("--proxy", help="Proxy URL, e.g. http://user:pass@host:port or socks5://host:port")]
_CookiesOpt = Annotated[Optional[str], typer.Option("--cookies", help="Path to a Netscape cookies.txt (msToken/ttwid/sessionid) for gated comments")]
_CookiesBrowserOpt = Annotated[Optional[str], typer.Option("--cookies-from-browser", help="Read cookies from an installed browser, e.g. 'chrome' or 'edge'")]


def _fetch_opts(proxy: Optional[str], cookies: Optional[str],
                cookies_from_browser: Optional[str]) -> dict:
    """Build the engine fetch kwargs from the shared CLI options (None values omitted)."""
    opts: dict = {}
    if proxy:
        opts["proxy"] = proxy
    if cookies:
        opts["cookiefile"] = cookies
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    return opts


# ── vaulted TikTok session (auto-restore after a one-time `navig tt login`) ────────────

_SESSION_HOST = "tiktok.com"

# Tri-state login flag shared by the browser-tier commands: default (None) auto-restores
# a saved session if you've run `navig tt login`; --login forces it; --anon forces anonymous.
_LoginOpt = Annotated[Optional[bool], typer.Option(
    "--login/--anon",
    help="Use your saved TikTok session (default: auto after `navig tt login`; --anon = anonymous)")]


def _has_tiktok_session() -> bool:
    """True if a TikTok session is saved in the slot the restore path actually reads.

    Uses ``list_sessions`` (plaintext metadata — no decrypt, so it won't prompt to unlock
    the vault) and matches the same *default* slot ``get_session("tiktok.com")`` reads, so a
    stale session saved under another username doesn't count as usable.
    """
    try:
        from navig.vault.logins import normalize_host
        from navig.vault.sessions import list_sessions

        host = normalize_host(_SESSION_HOST)
        return any(
            s.get("domain") == host and (s.get("username") or "default") == "default"
            for s in list_sessions()
        )
    except Exception:  # noqa: BLE001 — vault absent/locked → treat as "no usable session"
        return False


def _resolve_session(login: Optional[bool], *, announce: bool = True) -> Optional[str]:
    """Decide whether the browser tier restores the vaulted TikTok session.

    ``login`` is tri-state: ``None`` = auto (restore iff a session is saved), ``True``
    (``--login``) = force, ``False`` (``--anon``) = never. Returns the session host to
    restore, or ``None`` for an anonymous fetch.
    """
    if login is False:  # --anon: explicit anonymous
        return None
    if login is True:  # --login: explicit force (restore is best-effort downstream)
        return _SESSION_HOST
    # auto (default): only when a usable session is actually saved
    if _has_tiktok_session():
        if announce:
            ch.dim("🔓 using your saved TikTok session (--anon to browse anonymously)")
        return _SESSION_HOST
    return None


@tiktok_app.command("info")
def tiktok_info(
    url: Annotated[str, typer.Argument(help="TikTok URL (video or /photo/)")],
    as_json: Annotated[bool, typer.Option("--json", help="Print the full metadata as JSON (for scripts/agents)")] = False,
    browser: Annotated[bool, typer.Option("--browser", "-b", help="Read via the stealth browser (needed for /photo/ posts — auto-enabled for those)")] = False,
    headful: Annotated[bool, typer.Option("--headful", help="Show the browser window (with --browser)")] = False,
    login: _LoginOpt = None,
    proxy: _ProxyOpt = None,
    cookies: _CookiesOpt = None,
    cookies_from_browser: _CookiesBrowserOpt = None,
) -> None:
    """Show metadata: creator, description, stats (and image URLs for /photo/ posts).

    yt-dlp handles videos. TikTok **photo posts** (``/photo/…``) aren't supported by yt-dlp
    at all, so those read the page's own server-rendered data instead — over plain HTTP,
    falling back to the stealth browser only when TikTok didn't render it.
    """
    from navig_download.tiktok import engine

    # `is_photo_post` resolves a share link first; the raw-string check this used
    # to do said "video" for every shared photo post, whose path carries nothing.
    is_photo = engine.is_photo_post(url)
    if browser:
        res = _browser_post(url, top=0, headless=not headful, proxy=proxy, login=login, rounds=2)
        _present_info(res["meta"], as_json)
        return
    if is_photo:
        # One HTTP request reads the description, the stats AND every slide —
        # yt-dlp reports only the cover, and a browser launch costs ~10s more.
        meta = engine.read_post_http(url)
        if meta:
            _present_info(meta, as_json)
            return
        ch.info("TikTok didn't server-render this post — reading via the stealth browser …")
        res = _browser_post(url, top=0, headless=not headful, proxy=proxy, login=login, rounds=2)
        _present_info(res["meta"], as_json)
        return

    if not _require_ytdlp():
        raise typer.Exit(1)
    try:
        meta = engine.info(url, **_fetch_opts(proxy, cookies, cookies_from_browser))
    except engine.TikTokLoginRequired:
        # Not a bot-wall. The browser tier below CAN read an age gate when a session
        # is present, so this states the cause and then lets that run — an imperative
        # "run `navig tt login`" printed immediately above a successful read is
        # advice that contradicts the very next line of output.
        ch.warning("TikTok serves this post only to a logged-in account (age-gated or private).")
        ch.info("Reading via the stealth browser with your saved session — "
                "run `navig tt login` if this fails.")
    except engine.TikTokBlocked:
        ch.warning("yt-dlp was blocked — falling back to the stealth browser …")
    except Exception:  # noqa: BLE001 — a post yt-dlp cannot read at all (private, deleted,
        # region-blocked, or an extractor bump) → let the browser try. Photo posts no longer
        # land here: `info` canonicalizes /photo/ to the /video/ form yt-dlp accepts.
        ch.info("yt-dlp couldn't read this URL — reading via the stealth browser …")
    else:
        _present_info(meta, as_json)
        return
    res = _browser_post(url, top=0, headless=not headful, proxy=proxy, login=login, rounds=2)
    _present_info(res["meta"], as_json)


def _looks_unreadable(meta: dict) -> bool:
    """True when the browser/yt-dlp read essentially NOTHING — a private/deleted/region-blocked
    post, or a page that didn't render (SSR blob + item-detail API both yielded no data). The
    ``uploader`` can still be filled from the URL path, so it's excluded from the check."""
    return not (meta.get("id") or meta.get("description") or (meta.get("images"))
                or meta.get("view_count") or meta.get("like_count")
                or meta.get("comment_count"))


def _present_info(meta: dict, as_json: bool) -> bool:
    """Print a post's metadata. Returns ``False`` when the post was unreadable (so callers can
    skip a comments section etc.). ``--json`` always emits the raw dict for scripts."""
    if as_json:
        import json

        print(json.dumps(meta, ensure_ascii=False, indent=2))
        return not _looks_unreadable(meta)
    if _looks_unreadable(meta):
        ch.warning("Couldn't read this post — it may be private, deleted, region-blocked, or the "
                   "page didn't load.")
        ch.info("Try `--headful` to watch it, `--login` for gated posts, or a `--proxy`.")
        if meta.get("url"):
            ch.console.print(f"[dim]{meta['url']}[/dim]")
        return False
    kind = "🖼️ Photo" if meta.get("is_photo") else "🎵"
    ch.console.print(f"[bold]{kind} {meta.get('uploader') or 'TikTok'}[/bold]"
                     + (f"  ·  🌍 {meta['country']}" if meta.get("country") else ""))
    if meta.get("description"):
        ch.console.print(f"\n{meta['description'][:1000]}")
    stats = _fmt_stats(meta)
    if stats:
        ch.console.print(f"\n[dim]{stats}[/dim]")
    images = meta.get("images") or []
    if images:
        ch.console.print(f"\n[dim]{len(images)} image(s):[/dim]")
        for i, img in enumerate(images[:12], 1):
            ch.console.print(f"[dim]  {i}. {img}[/dim]")
    if meta.get("url"):
        ch.console.print(f"[dim]{meta['url']}[/dim]")
    return True


def _browser_post(url: str, *, top: int, headless: bool, proxy: Optional[str],
                  login: Optional[bool], rounds: int = 8) -> dict:
    """Run the stealth-browser post reader (description + optional comments), async→sync."""
    from navig_download.tiktok import browser_fetch

    session_host = _resolve_session(login)
    try:
        return asyncio.run(browser_fetch.fetch_post(
            url, max_comments=top, headless=headless, proxy=proxy, rounds=rounds,
            session_host=session_host))
    except browser_fetch.TikTokBrowserUnavailable as exc:
        ch.error(str(exc))
        raise typer.Exit(1) from exc
    except Exception as exc:  # noqa: BLE001 — the browser closed/crashed mid-fetch (don't crash the CLI)
        ch.error(f"The browser closed or crashed while reading the post ({str(exc)[:90]}).")
        ch.info("Try again, or `--headful` to watch it; `navig tt info <url>` for metadata only.")
        raise typer.Exit(1) from exc


@tiktok_app.command("post")
def tiktok_post(
    url: Annotated[str, typer.Argument(help="TikTok URL (video or /photo/)")],
    top: Annotated[int, typer.Option("--top", "-n", help="How many top comments")] = 10,
    as_json: Annotated[bool, typer.Option("--json", help="Print everything as JSON (for agents)")] = False,
    headful: Annotated[bool, typer.Option("--headful", help="Show the browser window")] = False,
    login: _LoginOpt = None,
    proxy: _ProxyOpt = None,
) -> None:
    """Read a whole post via the browser in ONE pass — description + stats + comments.

    The all-in-one headless fetcher. Works for videos AND /photo/ posts (which yt-dlp can't
    read). Comments may be login-gated in the EU — run `navig tt login` then pass --login.
    """
    res = _browser_post(url, top=top, headless=not headful, proxy=proxy, login=login)
    meta, comments = res["meta"], res.get("comments") or []
    if as_json:
        import json

        print(json.dumps({**meta, "comments": comments}, ensure_ascii=False, indent=2))
        return
    if not _present_info(meta, as_json=False):
        return  # unreadable post — the message + hint were already shown; no comments to add
    ch.console.print()
    if comments:
        ch.console.print(f"[bold]💬 Top {len(comments)} comments[/bold]")
        _print_comments(comments)
    elif meta.get("comment_count"):
        if _has_tiktok_session():
            ch.warning(f"{meta['comment_count']:,} comments exist but none loaded — your saved "
                       "session may have expired. Re-run `navig tt login` (or try --headful / a --proxy).")
        else:
            ch.warning(f"{meta['comment_count']:,} comments exist but none loaded — likely "
                       "login-gated (EU). Run `navig tt login` once and they'll load automatically.")
    else:
        ch.info("No comments loaded.")


@tiktok_app.command("login")
def tiktok_login(
    timeout: Annotated[int, typer.Option("--timeout", help="Seconds to wait for you to log in (interactive mode)")] = 240,
    engine: Annotated[str, typer.Option("--engine",
        help="Login browser engine: auto (default — tries camoufox/firefox, then a real Chrome) · "
             "camoufox (max stealth) · firefox · chrome (a REAL Chrome, looks non-automated) · "
             "chromium (Patchright)")] = "auto",
    from_browser: Annotated[Optional[str], typer.Option("--from-browser",
        help="Instead of logging in, import the session from a browser you're already logged "
             "into (firefox/chrome/edge/brave/…). Firefox is the most reliable on Windows.")] = None,
    browser_profile: Annotated[Optional[str], typer.Option("--profile",
        help="Browser profile to read cookies from (with --from-browser)")] = None,
) -> None:
    """Log into TikTok once and vault the session; afterwards every command uses it automatically.

    Two ways in:

    \b
    • default — opens a **Firefox** window and waits for you to log in. Firefox is driven over a
      non-CDP protocol, so it slips past TikTok's automated-login block (the region check that
      fails with "internal server error" in an automated Chrome). With `--engine auto` (default)
      it uses **Camoufox** when installed — a C++-stealth Firefox that also hides
      `navigator.webdriver`, so TikTok can't flag it as automation (plain Firefox still leaks
      that, which can trip the "Maximum number of attempts reached" limiter). `--engine chromium`
      for the old Patchright path.
    • `--from-browser firefox` — don't log in at all; import the session from a browser you're
      already logged into.

    Afterwards `navig tt post/comments/info <url>` use the session **automatically** (pass
    `--anon` to opt out). The password is never seen or stored — only the session cookies. Clear
    it any time with `navig tt logout`.
    """
    if _has_tiktok_session():
        ch.dim("You already have a saved TikTok session — this replaces it.")
    if from_browser:
        ok = _login_from_browser(from_browser, browser_profile)
    else:
        ok = asyncio.run(_do_tiktok_login(timeout, engine))
    if not ok:
        raise typer.Exit(1)


@tiktok_app.command("logout")
def tiktok_logout() -> None:
    """Forget the vaulted TikTok session (go back to anonymous browsing)."""
    try:
        from navig.vault.sessions import remove_session
    except Exception as exc:  # noqa: BLE001
        ch.error(f"vault unavailable: {exc}")
        raise typer.Exit(1) from exc
    if remove_session(_SESSION_HOST):
        ch.success("Removed your saved TikTok session — commands will now browse anonymously.")
    else:
        ch.info("No saved TikTok session to remove.")


def _build_login_controller(engine: str):
    """Build a HEADED controller for the interactive login. Returns (controller, engine_label).

    ``auto`` (default) picks Camoufox when installed (hides ``navigator.webdriver`` — plain
    Firefox can't), else plain Firefox. Both are non-CDP, so they slip past TikTok's
    automated-login region block; ``chromium`` keeps the old Patchright path. Each engine uses
    its own persistent profile so it stays "warm" (region cookies) across runs.
    """
    eng = (engine or "auto").strip().lower()
    if eng == "auto":
        from navig.browser.firefox import best_login_engine

        eng = best_login_engine()
        if eng == "firefox":
            ch.dim("Tip: `pip install camoufox[geoip]` for an undetectable login "
                   "(hides navigator.webdriver, which plain Firefox leaks).")
    if eng in ("firefox", "camoufox"):
        from navig.browser.firefox import FirefoxController

        profile = str(Path("~/.navig/browser/profiles/tiktok-firefox").expanduser())
        return FirefoxController(engine=eng, headless=False, user_data_dir=profile), eng
    if eng == "chrome":
        # A REAL system Chrome over CDP — no --enable-automation, so navigator.webdriver=false.
        # It looks like a normal browser (not stealth-patched), for a human-driven login when the
        # automation engines get bot-walled. Its own isolated profile; never the user's Chrome.
        from navig.browser.system_chrome import SystemChromeController

        profile = str(Path("~/.navig/browser/profiles/tiktok-chrome").expanduser())
        return SystemChromeController(headless=False, user_data_dir=profile), "chrome"
    from navig.browser.stealth import StealthConfig, StealthController

    return StealthController(StealthConfig(
        headless=False, user_data_dir="~/.navig/browser/profiles/tiktok")), "chromium"


# TikTok surfaces login-blocking errors as page TEXT (not a cookie or redirect). Detecting them
# lets the login stop waiting and explain, instead of a generic timeout after 30s of silence.
_LOGIN_ERROR_MARKERS: tuple[tuple[str, str], ...] = (
    ("maximum number of attempts",
     "TikTok is rate-limiting logins (\"maximum number of attempts reached\") — it blocks new "
     "attempts for a while (often ~24h). Wait, then run `navig tt login` again. Meanwhile you can "
     "import a session with `navig tt login --from-browser firefox`."),
    ("too many attempts",
     "TikTok is rate-limiting logins (too many attempts) — wait a while (often ~24h) and retry, "
     "or import a session with `navig tt login --from-browser firefox`."),
)


async def _detect_login_error(page) -> Optional[str]:
    """Best-effort scan of the login page for a known blocking error (e.g. the rate limit).
    Returns a ready-to-show explanation, or None. Never raises (page may be navigating/closed)."""
    try:
        text = await page.evaluate(
            "() => (document.body && document.body.innerText || '').toLowerCase()")
    except Exception:  # noqa: BLE001
        return None
    text = text or ""
    for marker, message in _LOGIN_ERROR_MARKERS:
        if marker in text:
            return message
    return None


async def _attempt_login(engine_request: str, timeout: int) -> tuple[str, str]:
    """One interactive-login attempt with a specific engine. Returns ``(result, engine_used)``:

    - ``"ok"`` — logged in and the session was vaulted.
    - ``"no_login"`` — the window opened but no login happened before the timeout (nothing to retry).
    - ``"browser_failed"`` — the browser couldn't launch OR closed/crashed mid-drive (retryable).

    Never raises on a browser failure — a crashed/closed browser (``Page.goto: Connection closed``)
    is caught and reported so the CLI degrades gracefully instead of crashing.
    """
    import time

    try:
        controller, engine = _build_login_controller(engine_request)
    except Exception as exc:  # noqa: BLE001
        ch.error(f"couldn't prepare the login browser: {exc}")
        return "browser_failed", engine_request

    # The Camoufox binary is fetched on first use — tell the user before it blocks the command.
    if engine == "camoufox":
        try:
            from navig.browser.firefox import camoufox_binary_available

            if not camoufox_binary_available():
                ch.info("Setting up the Camoufox stealth browser (one-time ~150MB download) — "
                        "please wait …")
        except Exception:  # noqa: BLE001
            pass

    try:
        await controller.start()
    except Exception as exc:  # noqa: BLE001 — launch failure (FirefoxUnavailable / driver)
        ch.warning(f"The {engine} login browser couldn't start ({str(exc)[:90]}).")
        try:
            await controller.stop()
        except Exception:  # noqa: BLE001
            pass
        return "browser_failed", engine

    try:
        page = controller.page
        # Warm the profile first: a plain visit seeds ttwid/region cookies so TikTok skips the
        # region bootstrap that dies under automation on a cold context.
        try:
            await page.goto("https://www.tiktok.com/", wait_until="domcontentloaded")
            await asyncio.sleep(2)
        except Exception:  # noqa: BLE001 — warm step is best-effort
            pass
        await page.goto("https://www.tiktok.com/login", wait_until="domcontentloaded")
        ch.info(f"A {engine} browser opened. Log into TikTok — I'll detect it and save the session.")
        if engine in ("firefox", "camoufox"):
            ch.dim("(Firefox avoids the automated-login block that fails in Chrome.)")
        # Poll for the sessionid cookie (set once logged in), up to the timeout.
        deadline = time.monotonic() + max(30, timeout)
        logged_in = False
        while time.monotonic() < deadline:
            cookies = await controller.context.cookies()
            if any(c.get("name") == "sessionid" and c.get("value") for c in cookies):
                logged_in = True
                break
            # Stop early + explain if TikTok shows a blocking error (e.g. the rate limit),
            # instead of waiting out the full timeout in silence.
            blocked = await _detect_login_error(page)
            if blocked:
                ch.warning(blocked)
                return "no_login", engine
            await asyncio.sleep(2)
        if not logged_in:
            ch.warning("Didn't detect a login before the timeout — nothing saved. (If you saw "
                       "\"maximum attempts reached\", that's a ~24h rate lock — wait and retry.)")
            return "no_login", engine
        state = await controller.context.storage_state()
        from navig.vault.sessions import save_session

        # Save to the DEFAULT slot — the exact one get_session("tiktok.com") restores from.
        # (Saving under a username="tiktok" slot here would never be found on restore.)
        save_session("tiktok.com", state)
        n = len(state.get("cookies") or [])
        ch.success(f"Saved your TikTok session ({n} cookies) to the vault.")
        ch.info("Done — `navig tt post/comments/info <url>` now use it automatically "
                "(pass --anon to browse without it). Remove it with `navig tt logout`.")
        return "ok", engine
    except Exception as exc:  # noqa: BLE001 — the browser window was closed or crashed mid-drive
        ch.warning(f"The {engine} browser closed or crashed before login finished "
                   f"({str(exc)[:90]}).")
        # A dead DRIVER is not a closed window. Camoufox declares an unpinned
        # `playwright`, so pip can pair a Juggler with a Firefox it does not speak;
        # the driver then dies mid-navigation and every future login repeats the
        # crash. Remember it against this exact package pair so `auto` stops
        # choosing Camoufox until one of them is upgraded. Matching the driver
        # string specifically is what keeps an ordinary window-close - the common
        # case - from disabling the stealthiest engine.
        if engine == "camoufox" and "reading from the driver" in str(exc).lower():
            from navig.browser.firefox import record_camoufox_incompatible

            record_camoufox_incompatible(
                f"the Playwright driver died mid-navigation ({str(exc)[:80]})")
            ch.dim("Skipping camoufox next time - its Playwright and Firefox builds "
                   "disagree. Upgrading either package re-enables it automatically.")
        return "browser_failed", engine
    finally:
        try:
            await controller.stop()
        except Exception:  # noqa: BLE001
            pass


async def _do_tiktok_login(timeout: int, engine: str = "auto") -> bool:
    requested = (engine or "auto").strip().lower()

    # Build the attempt ladder. An explicit engine tries just that one. `auto` escalates
    # stealthiest → most-reliable: Camoufox/Firefox (non-CDP, undetectable/region-safe) → a REAL
    # Chrome over CDP (no --enable-automation, so navigator.webdriver=false → looks non-automated)
    # as the safety net when the automation engines get bot-walled or crash.
    if requested == "auto":
        from navig.browser.firefox import best_login_engine

        ladder = [best_login_engine()]  # camoufox if installed, else firefox
        if "firefox" not in ladder:
            ladder.append("firefox")
        ladder.append("chrome")
    else:
        ladder = [requested]

    result, used = "browser_failed", requested
    for i, eng in enumerate(ladder):
        if i > 0:
            ch.info("Trying a real Chrome (looks non-automated) …" if eng == "chrome"
                    else f"Trying {eng} …")
        result, used = await _attempt_login(eng, timeout)
        # Only escalate when the BROWSER itself failed to run; "ok"/"no_login" mean the browser
        # worked (login succeeded, or was genuinely blocked/timed-out — another engine won't help).
        if result != "browser_failed":
            break

    if result == "ok":
        return True
    if result == "no_login":
        return False  # already warned
    ch.error("Login didn't complete — no browser engine could run.")
    ch.info("Retry, install an engine (`python -m playwright install firefox`), or import a "
            "session you're already logged into: `navig tt login --from-browser firefox`.")
    return False


# ── import a session from an installed browser (no automated login) ────────────────────

def _norm_samesite(v: object) -> str:
    """Normalise a cookie SameSite to Playwright's {Strict, Lax, None} (default Lax)."""
    s = str(v or "").strip().lower()
    if s == "strict":
        return "Strict"
    if s in ("none", "no_restriction"):
        return "None"
    return "Lax"


def _cookiejar_to_playwright(jar) -> list[dict]:
    """Convert a cookiejar to Playwright ``storageState`` cookies — TikTok domains only."""
    out: list[dict] = []
    for ck in jar:
        domain = ck.domain or ""
        if "tiktok" not in domain.lower():
            continue
        has = getattr(ck, "has_nonstandard_attr", lambda _k: False)
        get = getattr(ck, "get_nonstandard_attr", lambda _k: None)
        out.append({
            "name": ck.name,
            "value": ck.value or "",
            "domain": domain,
            "path": ck.path or "/",
            "expires": float(ck.expires) if getattr(ck, "expires", None) else -1,
            "httpOnly": bool(has("HttpOnly") or has("httponly")),
            "secure": bool(getattr(ck, "secure", False)),
            "sameSite": _norm_samesite(get("SameSite") if has("SameSite") else None),
        })
    return out


_CHROMIUM_BROWSERS = ("chrome", "edge", "brave", "chromium")


def _has_sessionid(cookies) -> bool:
    return any(c.get("name") == "sessionid" and c.get("value") for c in (cookies or []))


def _cookies_via_ytdlp(name: str, profile: Optional[str]) -> list[dict]:
    """Fast path: read the browser's cookie DB directly (plaintext for Firefox; works for a
    fully-closed pre-ABE Chrome). Returns [] on any failure (locked DB / App-Bound Encryption)."""
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except Exception:  # noqa: BLE001
        return []

    class _QuietLogger:  # keep yt-dlp's cookie chatter out of our output
        def debug(self, *a, **k):
            pass

        info = warning = error = debug

    try:
        jar = extract_cookies_from_browser(name, profile=profile, logger=_QuietLogger())
    except Exception:  # noqa: BLE001 — locked DB / ABE / unsupported browser
        return []
    return _cookiejar_to_playwright(jar)


def _cookies_via_cdp_capture(name: str) -> list[dict]:
    """ABE fallback for Chromium browsers: let the REAL browser hand us its own decrypted cookies
    over CDP (copies a minimal profile slice; never touches the live browser). See
    ``navig.browser.system_chrome.capture_existing_cookies``."""
    try:
        from navig.browser.system_chrome import capture_existing_cookies

        return asyncio.run(capture_existing_cookies(name, "tiktok"))
    except Exception as exc:  # noqa: BLE001
        ch.dim(f"(couldn't capture via CDP: {str(exc).splitlines()[0][:80]})")
        return []


def _vault_captured_session(cookies: list[dict], source: str) -> bool:
    try:
        from navig.vault.sessions import save_session

        save_session(_SESSION_HOST, {"cookies": cookies, "origins": []})
    except Exception as exc:  # noqa: BLE001
        ch.error(f"Couldn't save the session to the vault: {exc}")
        return False
    ch.success(f"Imported your TikTok session from {source} ({len(cookies)} cookies).")
    ch.info("Done — `navig tt post/comments/info <url>` now use it automatically "
            "(pass --anon to browse without it). Remove it with `navig tt logout`.")
    return True


def _login_from_browser(browser: str, profile: Optional[str]) -> bool:
    """Import a TikTok session from an installed browser you're already logged into — no login.

    Firefox stores cookies in plaintext, so it's read directly (most reliable). Chrome/Edge/Brave
    use App-Bound Encryption; when the direct read fails, we fall back to the **CDP capture** —
    copy a minimal profile slice and let the *real* browser decrypt its own cookies over CDP
    (App-Bound keys are bound to the user + binary, not the profile path). This never touches your
    live browser or profile, and only TikTok's session cookies are stored — never a password.
    """
    name = (browser or "").strip().lower()

    cookies = _cookies_via_ytdlp(name, profile)  # fast: Firefox, or a closed pre-ABE Chrome
    if not _has_sessionid(cookies) and name in _CHROMIUM_BROWSERS:
        ch.info(f"Reading {browser}'s encrypted cookies via a private copy (App-Bound "
                "Encryption) — your live browser and profile are untouched …")
        cookies = _cookies_via_cdp_capture(name)

    if not _has_sessionid(cookies):
        ch.warning(f"Couldn't get a logged-in TikTok session from {browser}.")
        ch.info("Make sure you're logged into TikTok there. Firefox is the most reliable "
                "(`navig tt login --from-browser firefox`); `--profile <name>` picks a profile.")
        return False

    return _vault_captured_session(cookies, browser)


@tiktok_app.command("comments")
def tiktok_comments(
    url: Annotated[str, typer.Argument(help="TikTok URL")],
    top: Annotated[int, typer.Option("--top", "-n", help="How many top comments")] = 10,
    headful: Annotated[bool, typer.Option("--headful", help="Show the browser window — harder to detect")] = False,
    no_cloud: Annotated[bool, typer.Option("--no-cloud", help="Don't use the Tier-C cloud browser even if configured")] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Emit raw JSON (comments + tier/blocked/healed/comment_count) for scripts")] = False,
    browser: Annotated[bool, typer.Option("--browser", "-b", hidden=True, help="Deprecated — the self-healing ladder is always used now")] = False,
    login: _LoginOpt = None,
    proxy: _ProxyOpt = None,
    cookies: _CookiesOpt = None,
    cookies_from_browser: _CookiesBrowserOpt = None,
) -> None:
    """List the top comments, ranked by likes.

    Runs the self-healing ladder automatically: yt-dlp (fast) → stealth browser (reads TikTok's
    OWN signed comment JSON) → cloud, escalating until comments come back. TikTok's comment
    endpoint is signed, so the browser tier does the real work — keep a session with
    ``navig tt login`` (comments are login-gated in the EU).
    """
    res = _comments_via_ladder(url, top=top, headless=not headful, proxy=proxy,
                               cookies=cookies, cookies_from_browser=cookies_from_browser,
                               allow_cloud=not no_cloud, login=login, quiet=json_out)
    _show_ladder_comments(res, as_json=json_out)


def _print_comments(comments: list) -> None:
    """Render comments as a house-style Rich table (ranked, one wrappable text column)."""
    from navig.console_helper import Table  # noqa: PLC0415
    from rich.text import Text  # noqa: PLC0415

    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("#", no_wrap=True, justify="right", style="dim")
    table.add_column("❤", no_wrap=True, justify="right", style="green")
    table.add_column("author", no_wrap=True, style="cyan")
    table.add_column("comment", overflow="fold")  # the single free-text column, wraps on narrow terminals
    for i, c in enumerate(comments, 1):
        author = f"@{c['author']}" if c.get("author") else "—"
        table.add_row(str(i), f"{c.get('likes', 0):,}", Text(author), Text((c.get("text") or "").strip()))
    ch.console.print(table)


def _show_ladder_comments(res: dict, *, as_json: bool = False) -> None:
    """Present a self-healing-ladder result; ``--json`` emits raw data, else a table + summary.

    Human mode exits 2 when nothing came back (so scripts can branch on it); JSON mode always
    prints valid JSON (empty ``comments`` + the block/tier state) and exits 0.
    """
    comments = res.get("comments") or []
    if as_json:
        import json  # noqa: PLC0415

        print(json.dumps({
            "comments": comments,
            "tier": res.get("tier"),
            "blocked": bool(res.get("blocked")),
            "healed": bool(res.get("healed")),
            "comment_count": res.get("comment_count"),
        }, ensure_ascii=False, indent=2))
        return
    if not comments:
        count = res.get("comment_count")
        if res.get("blocked"):
            if count:
                ch.warning(f"{count:,} comments exist but none came through — TikTok gated the "
                           "signed endpoint across every tier.")
            else:
                ch.warning("Couldn't read comments — TikTok gated the signed comment endpoint.")
            # Session-aware next step: comments are login-gated in the EU, so a session is the fix.
            if not _has_tiktok_session():
                ch.info("Run `navig tt login` once (comments are login-gated in the EU), then retry.")
            else:
                ch.info("Your saved session may have expired — re-run `navig tt login`, "
                        "or try --headful / a --proxy.")
        else:
            ch.warning("This post genuinely has no comments.")
        raise typer.Exit(2)
    _print_comments(comments)
    total = res.get("comment_count")
    healed = " · healed via browser" if res.get("healed") else ""
    suffix = f" of {total:,}" if total else ""
    ch.info(f"{len(comments)} comment(s){suffix} · tier {res.get('tier')}{healed}")


def _comments_via_ladder(url: str, *, top: int, headless: bool, proxy: Optional[str],
                         cookies: Optional[str], cookies_from_browser: Optional[str],
                         allow_cloud: bool, login: Optional[bool] = None,
                         quiet: bool = False) -> dict:
    """Run the self-healing tier ladder (yt-dlp → stealth browser → cloud).

    ``quiet`` suppresses the progress line so ``--json`` output stays machine-clean.
    """
    from navig_download.tiktok import fetch

    if not quiet:
        ch.info("Fetching comments (self-healing ladder: yt-dlp → browser → cloud) …")
    # proxy is passed once (explicit) — it drives the browser tiers and is forwarded into
    # Tier-A by the orchestrator; here we only add the cookie opts.
    cookie_opts = _fetch_opts(None, cookies, cookies_from_browser)
    return asyncio.run(fetch.fetch_comments(
        url, max_comments=top, headless=headless, proxy=proxy, allow_cloud=allow_cloud,
        session_host=_resolve_session(login), **cookie_opts))


@tiktok_app.command("analyse")
def tiktok_analyse(
    url: Annotated[str, typer.Argument(help="TikTok URL")],
    comments: Annotated[int, typer.Option("--comments", "-c", help="Comments to weigh")] = 20,
    login: _LoginOpt = None,
    proxy: _ProxyOpt = None,
    cookies: _CookiesOpt = None,
    cookies_from_browser: _CookiesBrowserOpt = None,
) -> None:
    """AI markdown briefing of a post (description + best comments combined).

    TikTok's comment endpoint is signed, so the top comments are recovered via the stealth
    browser when yt-dlp is gated — keep a session with ``navig tt login`` for EU-gated comments.
    """
    if not _require_ytdlp():
        raise typer.Exit(1)
    from navig_download.tiktok import engine

    ch.info("Analysing (metadata + comments, then AI briefing) …")
    try:
        result = asyncio.run(engine.analyse(
            url, max_comments=comments, session_host=_resolve_session(login),
            **_fetch_opts(proxy, cookies, cookies_from_browser)))
    except engine.TikTokLoginRequired:
        # Do NOT give up here. The browser tier reads age-gated posts that yt-dlp
        # cannot see at all — measured on a live one — so exiting would fail on a
        # post we can read whenever the operator is signed in. The generic branch
        # below always escalated; this one used to be the exception, which meant
        # the MORE precisely diagnosed failure got the WORSE handling.
        ch.warning("TikTok serves this post only to a logged-in account (age-gated or private).")
        ch.info("Reading via the stealth browser with your saved session — "
                "run `navig tt login` if this fails.")
        _brief_via_browser(url, comments=comments, proxy=proxy, login=login)
        return
    except engine.TikTokBlocked as exc:
        ch.error(f"TikTok blocked the request (bot-wall): {exc}")
        ch.info("Retry with --cookies-from-browser chrome and/or --proxy.")
        raise typer.Exit(2) from exc
    except Exception:  # noqa: BLE001 — a post yt-dlp cannot read at all → brief off the browser
        # read instead. Photo posts no longer land here (canonicalized to /video/), so this
        # is now the genuinely-unreadable case: private, deleted, or an extractor bump.
        ch.info("yt-dlp can't read this post — reading + briefing via the stealth browser …")
        _brief_via_browser(url, comments=comments, proxy=proxy, login=login)
        return
    meta = result["meta"]
    if meta.get("comments_blocked"):
        ch.warning("Comments stayed gated even via the browser — the briefing uses the "
                   "description only. Run `navig tt login` if you haven't.")
    _render_analysis(meta, result["brief"])


def _brief_via_browser(url: str, *, comments: int, proxy: str | None, login) -> None:
    """Read a post with the stealth browser and render the full analysis.

    Shared by both escalation branches of ``analyse``. It used to live inline in
    the generic one only, so the *login-gated* branch — the case with the most
    precise diagnosis — was the one that exited instead of trying the tier that
    can actually read those posts.
    """
    from navig_download.tiktok import engine  # lazy, like every other command here

    res = _browser_post(url, top=comments, headless=True, proxy=proxy, login=login, rounds=8)
    if not _present_info(res["meta"], as_json=False):
        return  # unreadable post — message already shown
    meta = dict(res["meta"])
    meta["comments"] = res.get("comments") or []
    _render_analysis(meta, asyncio.run(engine.brief_meta(meta)))


def _render_analysis(meta: dict, brief: str) -> None:
    """Render a TikTok analysis: header · stats · AI briefing (or raw description) · top comments."""
    head = f"[bold]🎵 {meta.get('uploader') or 'TikTok'}[/bold]"
    if meta.get("country"):
        head += f"  ·  🌍 {meta['country']}"
    ch.console.print(head)
    stats = _fmt_stats(meta)
    if stats:
        ch.console.print(f"[dim]{stats}[/dim]\n")
    if brief:
        ch.console.print(brief)
    else:
        ch.warning("AI briefing unavailable (no model configured?) — showing raw data.")
        if meta.get("description"):
            ch.console.print(f"\n{meta['description'][:1000]}")
    cs = meta.get("comments") or []
    if cs:
        ch.console.print(f"\n[bold]💬 Top {len(cs)} comments[/bold]")
        _print_comments(cs)


# ── music links (song.link / Odesli cross-platform resolver) ──────────────────

@tiktok_app.command("music-links")
def music_links(
    url: Annotated[str, typer.Argument(help="A share URL from Spotify / Apple Music / YouTube Music / Deezer / Tidal / SoundCloud / …")],
    as_json: Annotated[bool, typer.Option("--json", help="Print the resolved links as JSON (for scripts/agents)")] = False,
    country: Annotated[str, typer.Option("--country", help="Two-letter market for store links (song.link userCountry)")] = "US",
) -> None:
    """Resolve a music link to the same track on every other platform (via song.link).

    Paste any Spotify / Apple Music / Deezer / Tidal / SoundCloud link and get back the
    matching link on all 18+ services — no API key (free Odesli API).
    """
    from navig_download.music_links import MusicResolveError, resolve_links

    try:
        result = resolve_links(url, country=country)
    except MusicResolveError as exc:
        ch.error(str(exc))
        raise typer.Exit(1) from exc

    if as_json:
        import json

        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    title, artist = result.get("title"), result.get("artist")
    if title and artist:
        ch.console.print(f"[bold]🎵 {title}[/bold]  ·  [dim]{artist}[/dim]\n")
    elif title:
        ch.console.print(f"[bold]🎵 {title}[/bold]\n")

    from rich.table import Table

    table = Table(box=None, show_header=False, padding=(0, 2))
    table.add_column("Platform", style="cyan", no_wrap=True)
    table.add_column("Link")
    for link in result["links"]:
        table.add_row(link["label"], link["url"])
    ch.console.print(table)
    ch.dim(f"{len(result['links'])} platforms · song.link")


# alias-friendly callback (so `navig tt …` can map here via registration)
tt_app = tiktok_app


# ── new naming ───────────────────────────────────────────────────────────────
# The engine is generic (yt-dlp handles YouTube/TikTok/… video·audio·files), so the
# app is also exposed as `navig download` / `dl`. `navig tiktok` / `tt` keep working.
download_app = tiktok_app
