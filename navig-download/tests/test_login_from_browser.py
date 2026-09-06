"""`navig tt login --from-browser`: import a TikTok session from an installed browser's
cookies (the reliable path when TikTok's automated web-login is bot-walled)."""

from __future__ import annotations

from navig_download.commands import download as dl


class _FakeCookie:
    """Minimal stand-in for an http.cookiejar.Cookie (the shape yt-dlp yields)."""

    def __init__(self, name, value, domain, *, path="/", secure=True, expires=None,
                 samesite=None, httponly=False):
        self.name = name
        self.value = value
        self.domain = domain
        self.path = path
        self.secure = secure
        self.expires = expires
        self._attrs = {}
        if samesite is not None:
            self._attrs["SameSite"] = samesite
        if httponly:
            self._attrs["HttpOnly"] = True

    def has_nonstandard_attr(self, k):
        return k in self._attrs

    def get_nonstandard_attr(self, k):
        return self._attrs.get(k)


# ── _cookiejar_to_playwright ──────────────────────────────────────────────────────

def test_cookiejar_conversion_filters_and_shapes():
    jar = [
        _FakeCookie("sessionid", "abc", ".tiktok.com", samesite="None", httponly=True,
                    expires=1893456000),
        _FakeCookie("ttwid", "xyz", ".tiktok.com", samesite="Lax"),
        _FakeCookie("other", "nope", ".example.com"),  # non-TikTok → dropped
    ]
    out = dl._cookiejar_to_playwright(jar)
    assert {c["name"] for c in out} == {"sessionid", "ttwid"}  # example.com excluded

    sid = next(c for c in out if c["name"] == "sessionid")
    assert sid["domain"] == ".tiktok.com"
    assert sid["sameSite"] == "None" and sid["httpOnly"] is True and sid["secure"] is True
    assert sid["expires"] == 1893456000.0

    ttwid = next(c for c in out if c["name"] == "ttwid")
    assert ttwid["sameSite"] == "Lax"
    assert ttwid["expires"] == -1  # no expiry → session cookie


def test_norm_samesite():
    assert dl._norm_samesite("Strict") == "Strict"
    assert dl._norm_samesite("none") == "None"
    assert dl._norm_samesite("no_restriction") == "None"
    assert dl._norm_samesite(None) == "Lax"
    assert dl._norm_samesite("weird") == "Lax"


# ── _login_from_browser ───────────────────────────────────────────────────────────

def test_login_from_browser_imports_session(monkeypatch):
    jar = [_FakeCookie("sessionid", "abc", ".tiktok.com"),
           _FakeCookie("ttwid", "x", ".tiktok.com")]
    monkeypatch.setattr("yt_dlp.cookies.extract_cookies_from_browser", lambda *a, **k: jar)
    saved = {}

    def fake_save(host, state, **k):
        saved["host"] = host
        saved["state"] = state
        return "item-id"

    monkeypatch.setattr("navig.vault.sessions.save_session", fake_save)

    assert dl._login_from_browser("firefox", None) is True
    assert saved["host"] == "tiktok.com"
    assert any(c["name"] == "sessionid" for c in saved["state"]["cookies"])
    assert saved["state"]["origins"] == []


def test_login_from_browser_no_sessionid_is_false(monkeypatch):
    jar = [_FakeCookie("ttwid", "x", ".tiktok.com")]  # cookies, but not logged in
    monkeypatch.setattr("yt_dlp.cookies.extract_cookies_from_browser", lambda *a, **k: jar)
    called = {"saved": False}
    monkeypatch.setattr("navig.vault.sessions.save_session",
                        lambda *a, **k: called.__setitem__("saved", True))
    assert dl._login_from_browser("firefox", None) is False
    assert called["saved"] is False  # nothing vaulted without a sessionid


def test_login_from_browser_unreadable_is_false(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("Could not copy Chrome cookie database")

    monkeypatch.setattr("yt_dlp.cookies.extract_cookies_from_browser", boom)

    async def _no_cookies(app, host, headless=True):
        return []  # CDP capture also finds nothing (not logged in / clone-decrypt failed)

    monkeypatch.setattr("navig.browser.system_chrome.capture_existing_cookies", _no_cookies)
    assert dl._login_from_browser("chrome", None) is False


def test_chrome_abe_falls_back_to_cdp_capture(monkeypatch):
    """When yt-dlp can't read Chrome's App-Bound-Encrypted DB, fall back to the CDP capture
    (real browser decrypts its own cookies) and vault the result."""
    def boom(*a, **k):
        raise RuntimeError("Failed to decrypt with DPAPI")  # ABE / v20 failure

    monkeypatch.setattr("yt_dlp.cookies.extract_cookies_from_browser", boom)

    async def _cdp_cookies(app, host, headless=True):
        assert app == "chrome" and host == "tiktok"
        return [{"name": "sessionid", "value": "abc", "domain": ".tiktok.com", "path": "/"},
                {"name": "ttwid", "value": "x", "domain": ".tiktok.com", "path": "/"}]

    monkeypatch.setattr("navig.browser.system_chrome.capture_existing_cookies", _cdp_cookies)
    saved = {}
    monkeypatch.setattr("navig.vault.sessions.save_session",
                        lambda host, state, **k: saved.update({"host": host, "state": state}) or "id")

    assert dl._login_from_browser("chrome", None) is True
    assert saved["host"] == "tiktok.com"
    assert any(c["name"] == "sessionid" for c in saved["state"]["cookies"])


def test_firefox_never_uses_cdp_capture(monkeypatch):
    """Firefox is plaintext — it must NOT trigger the Chromium-only CDP capture."""
    monkeypatch.setattr("yt_dlp.cookies.extract_cookies_from_browser", lambda *a, **k: [])
    called = {"cdp": False}

    async def _cdp(app, host, headless=True):
        called["cdp"] = True
        return []

    monkeypatch.setattr("navig.browser.system_chrome.capture_existing_cookies", _cdp)
    assert dl._login_from_browser("firefox", None) is False
    assert called["cdp"] is False  # firefox is not a Chromium browser


def test_login_from_browser_passes_profile(monkeypatch):
    seen = {}

    def capture(name, *, profile=None, logger=None, **k):
        seen["name"] = name
        seen["profile"] = profile
        return [_FakeCookie("sessionid", "abc", ".tiktok.com")]

    monkeypatch.setattr("yt_dlp.cookies.extract_cookies_from_browser", capture)
    monkeypatch.setattr("navig.vault.sessions.save_session", lambda *a, **k: "id")
    assert dl._login_from_browser("Firefox", "dev-edition") is True
    assert seen["name"] == "firefox"  # lower-cased
    assert seen["profile"] == "dev-edition"
