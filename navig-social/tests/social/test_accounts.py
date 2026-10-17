"""Publishing networks connect from the app, through the daemon.

``navig social connect`` opened a browser and prompted on the terminal, so the app could
only list these networks, never connect them. ``oauth.run_headless`` hands the flow's
authorize URL to the app instead, and waits for the pasted URL on paste-back providers.
The vault and config are dicts here — nothing touches a real vault.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

pytest.importorskip("aiohttp")

from navig_social.deck_routes import accounts  # noqa: E402
from navig_social.social import oauth  # noqa: E402


class _Req:
    def __init__(self, body=None, match_info=None, query=None):
        self._body = body
        self.match_info = match_info or {}
        self.query = query or {}

    async def json(self):
        return self._body


@pytest.fixture
def vault(monkeypatch):
    secrets: dict[str, str] = {}
    config: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(oauth, "read_secret", lambda path: secrets.get(path))
    monkeypatch.setattr(oauth, "write_path_secret", lambda path, v: secrets.__setitem__(path, v))
    monkeypatch.setattr(oauth, "write_provider_token", lambda p, tok, label: secrets.__setitem__(p, tok))
    monkeypatch.setattr(oauth, "delete_secret", lambda path: secrets.pop(path, None) is not None)
    monkeypatch.setattr(oauth, "set_config", lambda p, k, v: config.__setitem__((p, k), v))
    monkeypatch.setattr(
        "navig_social.social.credentials.get_config", lambda p, k: config.get((p, k))
    )
    monkeypatch.setattr(accounts, "_JOBS", {})
    monkeypatch.setattr(accounts, "_LAST_ERROR", {})
    return secrets, config


def _rows():
    return {r["id"]: r for r in accounts.service_rows()}


# ── headless flow ────────────────────────────────────────────────────────────


def test_headless_paste_flow_publishes_the_url_and_takes_the_reply():
    flow = oauth.HeadlessFlow(reply_timeout=5)
    out = {}

    def run():
        out["code"] = oauth.run_headless(flow, oauth._manual_authorize, "https://auth/x?state=s1", "s1")

    t = threading.Thread(target=run)
    t.start()
    assert flow.url_ready.wait(5)
    assert (flow.auth_url, flow.paste) == ("https://auth/x?state=s1", True)
    assert flow.give_reply("https://cb/?code=C0DE&state=s1")
    t.join(5)
    assert out["code"] == "C0DE"


def test_headless_paste_rejects_a_reply_for_another_sign_in():
    flow = oauth.HeadlessFlow(reply_timeout=5)
    flow.give_reply("https://cb/?code=C&state=someone-else")
    with pytest.raises(oauth.SocialOAuthError):
        oauth.run_headless(flow, oauth._manual_authorize, "https://auth", "mine")


def test_headless_never_prompts_to_choose():
    flow = oauth.HeadlessFlow()
    with pytest.raises(oauth.SocialOAuthError, match="several"):
        oauth.run_headless(flow, oauth._choose, "Pages", [{"name": "A"}, {"name": "B"}])


def test_headless_mode_is_only_on_its_own_thread():
    flow = oauth.HeadlessFlow()
    oauth.run_headless(flow, lambda: None)
    assert oauth._active_flow() is None


# ── rows ─────────────────────────────────────────────────────────────────────


def test_rows_list_every_network_with_its_action(vault):
    rows = _rows()
    for name in oauth.CONNECTORS:
        assert f"social:{name}" in rows
    assert rows["social:youtube"]["name"] == "YouTube channel"
    assert all(r["category"] == "social" for r in rows.values())


def test_a_network_without_app_credentials_asks_for_them(vault):
    row = _rows()["social:youtube"]
    assert row["status"] == "needs_setup"
    assert row["connect"]["ready"] is False
    assert [f["name"] for f in row["connect"]["fields"]] == ["youtube/client_id", "youtube/client_secret"]
    assert [f["secret"] for f in row["connect"]["fields"]] == [False, True]


def test_instagram_asks_for_the_facebook_app(vault):
    labels = [f["label"] for f in _rows()["social:instagram"]["connect"]["fields"]]
    assert labels == ["Facebook App ID", "Facebook App secret"]


def test_with_app_credentials_it_is_ready_and_connected_once_a_token_exists(vault):
    secrets, config = vault
    secrets.update({"youtube/client_id": "id", "youtube/client_secret": "s"})
    row = _rows()["social:youtube"]
    assert (row["status"], row["connect"]["ready"]) == ("available", True)
    assert row["connect"]["start"] == "/api/deck/studio/networks/youtube/connect"
    secrets["youtube"] = "tok"
    config[("youtube", "account_label")] = "My Channel"
    row = _rows()["social:youtube"]
    assert row["status"] == "connected"
    assert row["accounts"][0]["label"] == "My Channel"


def test_threads_is_a_paste_back_flow(vault):
    connect = _rows()["social:threads"]["connect"]
    assert connect["paste"] is True and connect["reply"].endswith("/threads/connect/reply")


def test_devto_takes_a_key(vault):
    connect = _rows()["social:devto"]["connect"]
    assert connect["kind"] == "api_key" and connect["fields"][0]["name"] == "devto"


# ── connect job ──────────────────────────────────────────────────────────────


def test_connect_returns_the_url_and_the_job_finishes(vault, monkeypatch):
    secrets, config = vault

    def fake_connect(*, manual=False, timeout=240.0):
        oauth._active_flow().publish("https://accounts.google.com/o/oauth2/v2/auth?x", paste=False)
        from navig_social.social.accounts import save_account

        slot = save_account("youtube", "UC1", "My Channel", "tok")
        return oauth.ConnectResult("youtube", "My Channel", "1 hour", slot=slot)

    monkeypatch.setitem(oauth.CONNECTORS, "youtube", fake_connect)
    resp = asyncio.run(accounts.handle_network_connect(_Req(None, {"network": "youtube"})))
    assert resp.status == 200
    job = next(iter(accounts._JOBS.values()))
    for _ in range(100):
        if job.state != "running":
            break
        threading.Event().wait(0.02)
    assert job.state == "done"
    assert job.account == "My Channel"
    assert config[("youtube", "account_label")] == "My Channel"
    assert _rows()["social:youtube"]["accounts"][0]["label"] == "My Channel"


def test_a_connect_that_fails_at_once_says_why(vault, monkeypatch):
    def fake_connect(*, manual=False, timeout=240.0):
        raise oauth.SocialOAuthError("missing app credentials in the vault: youtube/client_id")

    monkeypatch.setitem(oauth.CONNECTORS, "youtube", fake_connect)
    resp = asyncio.run(accounts.handle_network_connect(_Req(None, {"network": "youtube"})))
    assert resp.status == 422
    assert "youtube/client_id" in _rows()["social:youtube"]["connect"]["last_error"]


def test_unknown_network_is_404(vault):
    resp = asyncio.run(accounts.handle_network_connect(_Req(None, {"network": "myspace"})))
    assert resp.status == 404


# ── credentials / disconnect ─────────────────────────────────────────────────


def test_saving_app_credentials_stores_only_accepted_paths(vault):
    secrets, _ = vault
    req = _Req({"values": {"youtube/client_id": " id ", "youtube/client_secret": "s"}}, {"network": "youtube"})
    assert asyncio.run(accounts.handle_network_credentials(req)).status == 200
    assert secrets == {"youtube/client_id": "id", "youtube/client_secret": "s"}

    bad = _Req({"values": {"linkedin/client_id": "x"}}, {"network": "youtube"})
    assert asyncio.run(accounts.handle_network_credentials(bad)).status == 400
    assert "linkedin/client_id" not in secrets


def test_a_rejected_devto_key_is_not_kept(vault, monkeypatch):
    secrets, _ = vault

    def reject():
        raise oauth.SocialOAuthError("401 unauthorized")

    monkeypatch.setattr(oauth, "connect_devto", reject)
    req = _Req({"values": {"devto": "bad"}}, {"network": "devto"})
    assert asyncio.run(accounts.handle_network_credentials(req)).status == 422
    assert "devto" not in secrets


def test_disconnect_removes_the_token_and_keeps_the_app(vault):
    secrets, _ = vault
    secrets.update({"youtube": "tok", "youtube/refresh_token": "rt", "youtube/client_id": "id"})
    resp = asyncio.run(accounts.handle_network_disconnect(_Req(None, {"network": "youtube"})))
    assert resp.status == 200
    assert secrets == {"youtube/client_id": "id"}


def test_rows_list_every_account_with_its_own_remove(vault):
    from navig_social.social.accounts import save_account

    save_account("youtube", "UC1", "Studio", "t1")
    save_account("youtube", "UC2", "Personal", "t2")
    row = _rows()["social:youtube"]
    assert row["multi_account"] is True
    assert [(a["label"], a["remove"]) for a in row["accounts"]] == [
        ("Studio", "/api/deck/studio/networks/youtube"),
        ("Personal", "/api/deck/studio/networks/youtube?account=UC2"),
    ]
    assert [a["status"] for a in row["accounts"]] == ["needs_reconnect", "needs_reconnect"]


def test_youtube_account_rows_report_refresh_readiness_individually(vault):
    from navig_social.social.accounts import save_account

    save_account("youtube", "UC1", "Studio", "t1", refresh="r1")
    save_account("youtube", "UC2", "Personal", "t2")
    row = _rows()["social:youtube"]
    assert [(a["id"], a["status"]) for a in row["accounts"]] == [
        ("UC1", "ok"), ("UC2", "needs_reconnect"),
    ]


def test_disconnecting_one_account_keeps_the_other(vault):
    from navig_social.social.accounts import list_accounts, save_account

    save_account("youtube", "UC1", "Studio", "t1")
    save_account("youtube", "UC2", "Personal", "t2")
    req = _Req(None, {"network": "youtube"}, {"account": "UC2"})
    assert asyncio.run(accounts.handle_network_disconnect(req)).status == 200
    assert [a["id"] for a in list_accounts("youtube")] == ["UC1"]


def test_a_rejected_devto_key_keeps_the_working_one(vault, monkeypatch):
    secrets, _ = vault
    secrets["devto"] = "good-key"

    def reject():
        raise oauth.SocialOAuthError("401 unauthorized")

    monkeypatch.setattr(oauth, "connect_devto", reject)
    req = _Req({"values": {"devto": "bad-key"}}, {"network": "devto"})
    assert asyncio.run(accounts.handle_network_credentials(req)).status == 422
    assert secrets["devto"] == "good-key"
