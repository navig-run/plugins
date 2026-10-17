"""`navig tt login` reaches the yt-dlp path, and `--anon` still means anonymous.

The vaulted session used to serve the BROWSER tier only: `_download_mixed` handed
`session_host` to the photo path and called `engine.download_urls` — the yt-dlp
video path — with no session and no cookies at all. So logging in could not unlock
an age-gated `/video/` post, which is what the operator tried to do.

yt-dlp reads cookies from a file, so the session has to touch the disk. The vault
exists to keep it encrypted at rest, so these pin the containment: a temp file with
owner-only permissions, one per process, removed at exit, and built only when a
session exists and the caller supplied nothing of its own.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from navig_download import anti_detect
from navig_download.tiktok import session_cookies as sc

#: Captured at IMPORT, before conftest's autouse stub replaces the module attribute
#: per test. Re-assigning the stub to itself restores nothing.
_REAL_VAULTED = sc.vaulted_cookiefile

_COOKIES = [
    {"name": "sessionid", "value": "abc", "domain": ".tiktok.com", "path": "/",
     "expires": 1893456000.0, "secure": True},
    {"name": "ttwid", "value": "xyz", "domain": "www.tiktok.com", "path": "/",
     "expires": -1, "secure": False},
]


@pytest.fixture
def vault(monkeypatch):
    """A saved session, and a fresh per-process cache for each test."""
    monkeypatch.setattr(sc, "_cached_path", None, raising=False)
    monkeypatch.setattr(sc, "vaulted_cookiefile", _REAL_VAULTED)  # undo conftest
    monkeypatch.setattr(
        "navig.vault.sessions.get_session",
        lambda host, *a, **kw: SimpleNamespace(storage_state={"cookies": list(_COOKIES)}),
    )


def test_the_session_becomes_a_netscape_file_yt_dlp_can_read(vault):
    path = _REAL_VAULTED()

    assert path and os.path.exists(path)
    text = open(path, encoding="utf-8").read()
    assert text.startswith("# Netscape HTTP Cookie File"), "yt-dlp rejects a file without this"
    rows = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert len(rows) == 2
    sess = next(r for r in rows if "sessionid" in r).split("\t")
    assert sess[0] == ".tiktok.com"
    assert sess[1] == "TRUE", "a leading dot means include-subdomains"
    assert sess[3] == "TRUE", "the secure flag was lost"
    assert sess[6] == "abc"


def test_a_session_cookie_becomes_0_not_a_1970_expiry(vault):
    """Playwright writes -1 for 'expires when the browser closes'. Passed through,
    yt-dlp would read a negative epoch and drop the cookie as long expired."""
    rows = open(_REAL_VAULTED(), encoding="utf-8").read().splitlines()
    ttwid = next(r for r in rows if "ttwid" in r).split("\t")
    assert ttwid[4] == "0"


def test_it_is_scratch_not_state_and_owner_only(vault):
    """The vault keeps this encrypted at rest; the bridge must not quietly create a
    durable plaintext copy next to the config."""
    import tempfile

    path = _REAL_VAULTED()
    assert os.path.dirname(path) == os.path.realpath(tempfile.gettempdir()) or \
        tempfile.gettempdir() in path, "the session was written outside the temp dir"
    if os.name != "nt":
        assert oct(os.stat(path).st_mode)[-3:] == "600"


def test_the_file_is_hardened_BEFORE_any_content_is_written(vault, monkeypatch):
    """The mode assertion above is skipped on Windows, where this project runs
    first — so the call itself is what gets pinned, on every platform. Creating
    the file world-readable and tightening it afterwards leaves a window where
    the session is on disk and readable by anyone.
    """
    order: list[str] = []
    import navig.core.file_permissions as fp

    real = fp.set_owner_only_file_permissions
    monkeypatch.setattr(fp, "set_owner_only_file_permissions",
                        lambda p: (order.append("harden"), real(p))[1])
    real_open = open

    def _spy_open(file, mode="r", *a, **kw):
        if "w" in mode and str(file).endswith(".cookies.txt"):
            order.append("write")
        return real_open(file, mode, *a, **kw)

    monkeypatch.setattr("builtins.open", _spy_open)
    _REAL_VAULTED()

    assert order[:2] == ["harden", "write"], f"hardening did not come first: {order}"


def test_one_file_per_process_not_one_per_button(vault):
    """A shared link is read up to five times; each read writing its own copy would
    multiply the window for no benefit."""
    assert _REAL_VAULTED() == _REAL_VAULTED()


def test_no_session_means_no_file(monkeypatch):
    monkeypatch.setattr(sc, "_cached_path", None, raising=False)
    monkeypatch.setattr("navig.vault.sessions.get_session", lambda *a, **kw: None)
    assert _REAL_VAULTED() is None


def test_an_empty_session_means_no_file(monkeypatch):
    monkeypatch.setattr(sc, "_cached_path", None, raising=False)
    monkeypatch.setattr("navig.vault.sessions.get_session",
                        lambda *a, **kw: SimpleNamespace(storage_state={"cookies": []}))
    assert _REAL_VAULTED() is None


def test_a_broken_vault_falls_back_to_anonymous_not_a_crash(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("vault locked")

    monkeypatch.setattr(sc, "_cached_path", None, raising=False)
    monkeypatch.setattr("navig.vault.sessions.get_session", _boom)
    assert _REAL_VAULTED() is None


class TestWiring:
    def test_the_session_is_used_when_nothing_else_is_set(self, vault):
        """The point of the bridge. Without this, deleting the fallback entirely
        passes every other test here — mutation testing proved exactly that."""
        _p, cf, _c = anti_detect.resolve_fetch_defaults()

        assert cf, "the vaulted session never reached yt-dlp"
        assert "sessionid" in open(cf, encoding="utf-8").read()

    def test_the_session_is_the_LAST_resort(self, vault, monkeypatch):
        """Anything the caller or operator states explicitly still wins."""
        monkeypatch.setattr(anti_detect, "vaulted_cookiefile", sc.vaulted_cookiefile,
                            raising=False)
        _p, cf, _c = anti_detect.resolve_fetch_defaults(cookiefile="/explicit.txt")
        assert cf == "/explicit.txt"

        _p, cf, cfb = anti_detect.resolve_fetch_defaults(cookiesfrombrowser="firefox")
        assert cfb == ("firefox",) and cf is None, "a browser jar was overridden by the vault"

    def test_anon_is_still_anonymous(self, vault):
        _p, cf, _c = anti_detect.resolve_fetch_defaults(use_session=False)
        assert cf is None, "--anon handed over the session anyway"

    def test_the_downloader_honours_the_namespace_flag(self, vault):
        """`--anon` reaches the three places the bundled downloader builds options.

        Takes the `vault` fixture on purpose: without a real session behind it the
        True case yields no cookiefile either, and the test passes whether or not
        the flag is honoured.
        """
        # `import navig_download.downloader.main as dl` binds the package's
        # re-exported `main` FUNCTION, which shadows the submodule of the same
        # name — the attribute lookup wins over the import. import_module is
        # unambiguous.
        import importlib

        dl = importlib.import_module("navig_download.downloader.main")

        assert dl._harden({}, use_session=True).get("cookiefile"), "premise: True hands it over"
        assert dl._harden({}, use_session=False).get("cookiefile") is None
