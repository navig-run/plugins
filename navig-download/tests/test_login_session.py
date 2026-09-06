"""TikTok vaulted-session auto-restore: tri-state resolver, slot matching, and a
regression test for the save/restore label mismatch that made `--login` a no-op."""

from __future__ import annotations

import asyncio

from navig_download.commands import download as dl


# ── _resolve_session (tri-state: None=auto, True=--login, False=--anon) ────────────

def test_resolve_session_anon_is_none(monkeypatch):
    monkeypatch.setattr(dl, "_has_tiktok_session", lambda: True)  # even with a session…
    assert dl._resolve_session(False) is None                     # …--anon forces anonymous


def test_resolve_session_login_forces_host(monkeypatch):
    monkeypatch.setattr(dl, "_has_tiktok_session", lambda: False)  # even with no session…
    assert dl._resolve_session(True) == "tiktok.com"              # …--login forces the host


def test_resolve_session_auto_uses_saved_session(monkeypatch):
    monkeypatch.setattr(dl, "_has_tiktok_session", lambda: True)
    assert dl._resolve_session(None, announce=False) == "tiktok.com"


def test_resolve_session_auto_none_when_no_session(monkeypatch):
    monkeypatch.setattr(dl, "_has_tiktok_session", lambda: False)
    assert dl._resolve_session(None, announce=False) is None


# ── _has_tiktok_session (matches only the default slot the restore path reads) ─────

def _patch_sessions(monkeypatch, rows):
    monkeypatch.setattr("navig.vault.sessions.list_sessions", lambda **kw: rows)


def test_has_session_true_for_default_slot(monkeypatch):
    _patch_sessions(monkeypatch, [{"domain": "tiktok.com", "username": None}])
    assert dl._has_tiktok_session() is True


def test_has_session_ignores_orphan_username_slot(monkeypatch):
    # A session saved under a non-default username is NOT what get_session("tiktok.com")
    # reads, so it must not count (this is the exact orphan the old bug would leave behind).
    _patch_sessions(monkeypatch, [{"domain": "tiktok.com", "username": "tiktok"}])
    assert dl._has_tiktok_session() is False


def test_has_session_false_for_other_domain(monkeypatch):
    _patch_sessions(monkeypatch, [{"domain": "example.com", "username": None}])
    assert dl._has_tiktok_session() is False


def test_has_session_false_when_empty(monkeypatch):
    _patch_sessions(monkeypatch, [])
    assert dl._has_tiktok_session() is False


def test_has_session_false_when_vault_raises(monkeypatch):
    def boom(**kw):
        raise RuntimeError("vault locked")

    monkeypatch.setattr("navig.vault.sessions.list_sessions", boom)
    assert dl._has_tiktok_session() is False


# ── regression: login must save to the slot restore reads ─────────────────────────

class _FakeCtx:
    async def cookies(self):
        return [{"name": "sessionid", "value": "abc"}]

    async def storage_state(self):
        return {"cookies": [{"name": "sessionid", "value": "abc"}], "origins": []}


class _FakePage:
    async def goto(self, *a, **kw):
        return None


class _FakeController:
    def __init__(self, *a, **kw):
        self.context = _FakeCtx()
        self.page = _FakePage()

    async def start(self):
        return None

    async def stop(self):
        return None


def test_login_saves_to_the_slot_restore_reads(monkeypatch):
    """The whole point of `navig tt login`: what it saves, restore must find.

    The old code saved under username="tiktok" but restore read the default slot, so the
    session was never found. Assert the label login saves to == the label get_session reads.
    """
    from navig.vault.sessions import session_label

    captured = {}

    def fake_save(domain, storage_state, *, username=None, **kw):
        captured["label"] = session_label(domain, username)
        return "item-id"

    monkeypatch.setattr("navig.browser.stealth.StealthController", _FakeController)
    monkeypatch.setattr("navig.browser.stealth.StealthConfig",
                        lambda **kw: object())
    monkeypatch.setattr("navig.vault.sessions.save_session", fake_save)

    # engine="chromium" drives the mocked StealthController path (the save-slot logic is
    # engine-independent; Firefox is exercised separately in test_login_engine.py).
    ok = asyncio.run(dl._do_tiktok_login(timeout=1, engine="chromium"))
    assert ok is True
    # restore reads get_session("tiktok.com") → the default slot:
    assert captured["label"] == session_label("tiktok.com")
    # and it must NOT be the old broken 'tiktok' slot:
    assert captured["label"] != session_label("tiktok.com", "tiktok")
