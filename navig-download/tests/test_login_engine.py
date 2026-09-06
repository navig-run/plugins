"""`navig tt login --engine <firefox|camoufox|chromium>` — engine selection for the
interactive login (Firefox is the default because it slips past TikTok's automated-login block)."""

from __future__ import annotations

import asyncio

from typer.testing import CliRunner

from navig_download.commands import download as dl


# ── fakes for the interactive login controller ────────────────────────────────────

class _FakePage:
    def __init__(self, crash=False, error_text=""):
        self._crash = crash
        self._error_text = error_text

    async def goto(self, url, *a, **k):
        # A crashed/closed browser raises this on navigation — the exact reported crash.
        if self._crash and "login" in url:
            raise RuntimeError("Page.goto: Connection closed while reading from the driver")
        return None

    async def evaluate(self, *a, **k):
        return (self._error_text or "").lower()  # login-page text the rate-limit detector scans


class _FakeCtx:
    def __init__(self, has_session=True):
        self._has_session = has_session

    async def cookies(self):
        return [{"name": "sessionid", "value": "abc"}] if self._has_session else []

    async def storage_state(self):
        return {"cookies": [{"name": "sessionid", "value": "abc"}], "origins": []}


class _FakeLoginCtl:
    def __init__(self, label, *, fail_start=False, crash_goto=False, error_text="",
                 has_session=True):
        self.label = label
        self._fail = fail_start
        self._crash_goto = crash_goto
        self._error_text = error_text
        self._has_session = has_session
        self.started = False
        self.stopped = False

    @property
    def page(self):
        return _FakePage(crash=self._crash_goto, error_text=self._error_text)

    @property
    def context(self):
        return _FakeCtx(has_session=self._has_session)

    async def start(self):
        self.started = True
        if self._fail:
            raise RuntimeError(f"{self.label} boom")

    async def stop(self):
        self.stopped = True


def test_build_login_controller_firefox():
    ctrl, engine = dl._build_login_controller("firefox")
    assert engine == "firefox"
    assert type(ctrl).__name__ == "FirefoxController"
    assert ctrl.engine_name == "firefox"


def test_build_login_controller_camoufox():
    ctrl, engine = dl._build_login_controller("camoufox")
    assert engine == "camoufox"
    assert type(ctrl).__name__ == "FirefoxController"
    assert ctrl.engine_name == "camoufox"


def test_build_login_controller_chromium():
    ctrl, engine = dl._build_login_controller("chromium")
    assert engine == "chromium"
    assert type(ctrl).__name__ == "StealthController"


def test_build_login_controller_auto_resolves_to_best_engine(monkeypatch):
    # auto → camoufox when its package is installed…
    monkeypatch.setattr("navig.browser.firefox.best_login_engine", lambda: "camoufox")
    ctrl, engine = dl._build_login_controller("auto")
    assert engine == "camoufox" and type(ctrl).__name__ == "FirefoxController"
    # …→ plain firefox otherwise
    monkeypatch.setattr("navig.browser.firefox.best_login_engine", lambda: "firefox")
    ctrl2, engine2 = dl._build_login_controller("auto")
    assert engine2 == "firefox" and type(ctrl2).__name__ == "FirefoxController"


def test_login_command_defaults_to_auto_and_passes_engine(monkeypatch):
    seen = {}

    async def fake_do(timeout, engine="auto"):
        seen["timeout"] = timeout
        seen["engine"] = engine
        return True

    monkeypatch.setattr(dl, "_do_tiktok_login", fake_do)
    monkeypatch.setattr(dl, "_has_tiktok_session", lambda: False)

    # default → auto (resolved to the best engine inside _build_login_controller)
    r = CliRunner().invoke(dl.tiktok_app, ["login", "--timeout", "5"])
    assert r.exit_code == 0
    assert seen["engine"] == "auto"

    # explicit override
    r = CliRunner().invoke(dl.tiktok_app, ["login", "--engine", "camoufox", "--timeout", "9"])
    assert r.exit_code == 0
    assert seen["engine"] == "camoufox" and seen["timeout"] == 9


def test_login_from_browser_still_bypasses_interactive(monkeypatch):
    # --from-browser must NOT trigger the interactive engine login
    called = {"interactive": False, "import": False}

    async def fake_do(timeout, engine="firefox"):
        called["interactive"] = True
        return True

    monkeypatch.setattr(dl, "_do_tiktok_login", fake_do)
    monkeypatch.setattr(dl, "_login_from_browser",
                        lambda b, p: called.__setitem__("import", True) or True)
    monkeypatch.setattr(dl, "_has_tiktok_session", lambda: False)

    r = CliRunner().invoke(dl.tiktok_app, ["login", "--from-browser", "firefox"])
    assert r.exit_code == 0
    assert called["import"] is True and called["interactive"] is False


# ── auto ladder: camoufox → firefox → real chrome (robustness) ─────────────────────

def _force_ladder_camoufox_first(monkeypatch):
    # Make the `auto` ladder deterministic: camoufox → firefox → chrome.
    monkeypatch.setattr("navig.browser.firefox.best_login_engine", lambda: "camoufox")


def test_login_auto_falls_back_to_firefox_when_camoufox_cant_start(monkeypatch):
    """`--engine auto` tries Camoufox first; if it can't start, escalate to plain Firefox
    (still beats the region wall) rather than failing the whole login."""
    built = []

    def fake_build(engine):
        built.append(engine)
        if engine == "camoufox":
            return _FakeLoginCtl("camoufox", fail_start=True), "camoufox"
        return _FakeLoginCtl(engine, fail_start=False), engine  # firefox succeeds

    _force_ladder_camoufox_first(monkeypatch)
    monkeypatch.setattr(dl, "_build_login_controller", fake_build)
    saved = {}
    monkeypatch.setattr("navig.vault.sessions.save_session",
                        lambda host, state, **k: saved.update({"host": host}) or "id")

    ok = asyncio.run(dl._do_tiktok_login(timeout=1, engine="auto"))
    assert ok is True
    assert built == ["camoufox", "firefox"]    # camoufox failed → firefox succeeded (chrome unused)
    assert saved.get("host") == "tiktok.com"


def test_login_auto_escalates_to_real_chrome(monkeypatch):
    """When BOTH camoufox and firefox fail (the user's exact case), `auto` escalates to a real
    Chrome — which logs in like a normal browser."""
    built = []

    def fake_build(engine):
        built.append(engine)
        if engine in ("camoufox", "firefox"):
            return _FakeLoginCtl(engine, crash_goto=True), engine  # both crash mid-drive
        return _FakeLoginCtl("chrome", fail_start=False), "chrome"  # real Chrome works

    _force_ladder_camoufox_first(monkeypatch)
    monkeypatch.setattr(dl, "_build_login_controller", fake_build)
    saved = {}
    monkeypatch.setattr("navig.vault.sessions.save_session",
                        lambda host, state, **k: saved.update({"host": host}) or "id")

    ok = asyncio.run(dl._do_tiktok_login(timeout=1, engine="auto"))
    assert ok is True
    assert built == ["camoufox", "firefox", "chrome"]  # escalated all the way to the real Chrome
    assert saved.get("host") == "tiktok.com"


def test_login_explicit_camoufox_does_not_escalate(monkeypatch):
    """An EXPLICIT `--engine camoufox` respects the choice — it fails instead of switching."""
    built = []

    def fake_build(engine):
        built.append(engine)
        return _FakeLoginCtl("camoufox", fail_start=True), "camoufox"

    monkeypatch.setattr(dl, "_build_login_controller", fake_build)

    ok = asyncio.run(dl._do_tiktok_login(timeout=1, engine="camoufox"))
    assert ok is False
    assert built == ["camoufox"]  # no escalation for an explicit engine choice


def test_login_no_login_does_not_escalate(monkeypatch):
    """If the browser WORKS but login is blocked/timed-out ("no_login"), don't try more engines —
    another browser won't beat a rate limit."""
    built = []

    def fake_build(engine):
        built.append(engine)
        # camoufox starts + drives fine, but the page shows the rate limit → "no_login" (fast,
        # via _detect_login_error — no waiting out the poll).
        return _FakeLoginCtl(engine, has_session=False,
                             error_text="Maximum number of attempts reached."), engine

    _force_ladder_camoufox_first(monkeypatch)
    monkeypatch.setattr(dl, "_build_login_controller", fake_build)

    ok = asyncio.run(dl._do_tiktok_login(timeout=1, engine="auto"))
    assert ok is False
    assert built == ["camoufox"]  # stopped after the first engine (browser worked; login didn't)


def test_login_browser_crash_explicit_engine_fails_cleanly(monkeypatch):
    """An explicit engine that crashes mid-drive returns False (no crash, no escalation)."""
    def fake_build(engine):
        return _FakeLoginCtl(engine, crash_goto=True), engine

    monkeypatch.setattr(dl, "_build_login_controller", fake_build)

    ok = asyncio.run(dl._do_tiktok_login(timeout=1, engine="chromium"))
    assert ok is False  # returned cleanly instead of raising


# ── login-page error detection (rate limit) ────────────────────────────────────────

class _TextPage:
    """A page whose innerText we control; evaluate() lower-cases it like the real JS does."""

    def __init__(self, text):
        self._text = text

    async def evaluate(self, *a, **k):
        return (self._text or "").lower()


async def test_detect_login_error_rate_limit():
    page = _TextPage("Log in\nMaximum number of attempts reached. Try again later.")
    msg = await dl._detect_login_error(page)
    assert msg is not None and "rate-limit" in msg.lower()
    assert "24h" in msg or "later" in msg.lower()


async def test_detect_login_error_too_many_attempts():
    page = _TextPage("Please wait — too many attempts.")
    assert await dl._detect_login_error(page) is not None


async def test_detect_login_error_none_on_clean_page():
    page = _TextPage("Log in\nEmail or username\nPassword\nForgot password?")
    assert await dl._detect_login_error(page) is None


async def test_detect_login_error_swallows_exceptions():
    class _Boom:
        async def evaluate(self, *a, **k):
            raise RuntimeError("page navigating / closed")

    assert await dl._detect_login_error(_Boom()) is None
