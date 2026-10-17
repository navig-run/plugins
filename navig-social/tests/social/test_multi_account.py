"""Several accounts per network: a second YouTube channel never replaces the first.

Connecting wrote the token into the single ``<provider>`` slot, so a second channel
overwrote the first. The first account keeps that slot (existing installs read unchanged);
others go beside it, and publishing picks one with the target ``"@<id>"``.
Secrets and config are dicts here — nothing touches a real vault or config file.
"""
from __future__ import annotations

import asyncio

import pytest

from navig_social.social import accounts, credentials, oauth, publishers
from navig_social.social.types import RenderedPost


@pytest.fixture
def store(monkeypatch):
    secrets: dict[str, str] = {}
    config: dict[tuple[str, str], object] = {}
    monkeypatch.setattr(oauth, "read_secret", lambda label: secrets.get(label))
    monkeypatch.setattr(oauth, "write_path_secret", lambda label, v: secrets.__setitem__(label, v))
    monkeypatch.setattr(oauth, "write_provider_token", lambda p, tok, title: secrets.__setitem__(p, tok))
    monkeypatch.setattr(oauth, "delete_secret", lambda label: secrets.pop(label, None) is not None)
    monkeypatch.setattr(oauth, "set_config", lambda p, k, v: config.__setitem__((p, k), v))
    monkeypatch.setattr(credentials, "get_config", lambda p, k: config.get((p, k)))
    monkeypatch.setattr(credentials, "_vault_secret", lambda label: secrets.get(label))
    for name in ("NAVIG_YOUTUBE_TOKEN", "YOUTUBE_TOKEN", "YOUTUBE_ACCESS_TOKEN",
                 "NAVIG_FACEBOOK_TOKEN", "FACEBOOK_TOKEN", "FACEBOOK_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return secrets, config


def test_the_second_channel_goes_beside_the_first(store):
    secrets, config = store
    assert accounts.save_account("youtube", "UC1", "Studio", "tok1", refresh="rt1") == "default"
    assert accounts.save_account("youtube", "UC2", "Personal", "tok2", refresh="rt2") == "UC2"
    assert secrets["youtube"] == "tok1" and secrets["youtube/refresh_token"] == "rt1"
    assert secrets["youtube@UC2"] == "tok2" and secrets["youtube@UC2/refresh_token"] == "rt2"
    assert [(a["id"], a["label"], a["default"]) for a in accounts.list_accounts("youtube")] == [
        ("UC1", "Studio", True), ("UC2", "Personal", False),
    ]


def test_reconnecting_the_same_account_refreshes_it_in_place(store):
    secrets, _ = store
    accounts.save_account("youtube", "UC1", "Studio", "old")
    accounts.save_account("youtube", "UC2", "Personal", "tok2")
    assert accounts.save_account("youtube", "UC2", "Personal", "new2") == "UC2"
    assert accounts.save_account("youtube", "UC1", "Studio", "new1") == "default"
    assert secrets["youtube"] == "new1" and secrets["youtube@UC2"] == "new2"
    assert len(accounts.list_accounts("youtube")) == 2


def test_an_install_from_before_accounts_reconnects_in_place(store):
    """A token with no recorded id is the old single-slot shape: keep the old behaviour."""
    secrets, config = store
    secrets["youtube"] = "legacy"
    assert accounts.save_account("youtube", "UC9", "Studio", "fresh") == "default"
    assert secrets["youtube"] == "fresh" and config[("youtube", "account_id")] == "UC9"
    assert accounts.extras("youtube") == []


def test_get_token_picks_the_account(store):
    accounts.save_account("youtube", "UC1", "Studio", "tok1")
    accounts.save_account("youtube", "UC2", "Personal", "tok2")
    assert credentials.get_token("youtube") == "tok1"
    assert credentials.get_token("youtube", "UC1") == "tok1"
    assert credentials.get_token("youtube", "UC2") == "tok2"
    assert credentials.get_token("youtube", "UC404") is None


def test_resolve_account_accepts_id_at_id_or_unique_label_and_never_falls_back(store):
    accounts.save_account("youtube", "UC1", "Studio", "tok1")
    accounts.save_account("youtube", "UC2", "Personal", "tok2")
    assert accounts.resolve_account("youtube", "UC2") == "UC2"
    assert accounts.resolve_account("youtube", "@UC2") == "UC2"
    assert accounts.resolve_account("youtube", "personal") == "UC2"
    assert accounts.resolve_account("youtube", "") is None
    with pytest.raises(ValueError, match="not connected"):
        accounts.resolve_account("youtube", "missing")


def test_resolve_account_rejects_an_ambiguous_label(store):
    accounts.save_account("youtube", "UC1", "Studio", "tok1")
    accounts.save_account("youtube", "UC2", "Studio", "tok2")
    with pytest.raises(ValueError, match="several youtube accounts"):
        accounts.resolve_account("youtube", "Studio")


def test_removing_the_first_account_promotes_the_next_with_its_settings(store):
    secrets, config = store
    accounts.save_account("facebook", "P1", "Shop", "t1", config={"page_id": "P1"})
    accounts.save_account("facebook", "P2", "Blog", "t2", config={"page_id": "P2"})
    assert accounts.remove_account("facebook") is True
    assert secrets.get("facebook") == "t2" and "facebook@P2" not in secrets
    assert config[("facebook", "page_id")] == "P2"
    assert [a["id"] for a in accounts.list_accounts("facebook")] == ["P2"]


def test_removing_another_account_leaves_the_first(store):
    secrets, _ = store
    accounts.save_account("youtube", "UC1", "Studio", "tok1")
    accounts.save_account("youtube", "UC2", "Personal", "tok2")
    assert accounts.remove_account("youtube", "UC2") is True
    assert secrets == {"youtube": "tok1"}
    assert [a["id"] for a in accounts.list_accounts("youtube")] == ["UC1"]


def test_removing_the_only_account_disconnects(store):
    accounts.save_account("youtube", "UC1", "Studio", "tok1")
    assert accounts.remove_account("youtube") is True
    assert accounts.list_accounts("youtube") == []


def test_list_targets_offers_one_chip_per_account(store):
    accounts.save_account("youtube", "UC1", "Studio", "tok1")
    pub = publishers.YouTubePublisher()
    assert len(asyncio.run(pub.list_targets())) == 1
    accounts.save_account("youtube", "UC2", "Personal", "tok2")
    targets = asyncio.run(pub.list_targets())
    assert [(t.target, t.display) for t in targets] == [("", "Studio"), ("@UC2", "Personal")]


def test_telegram_at_targets_stay_destinations():
    assert publishers.TelegramPublisher.multi_account is False
    assert publishers.TelegramPublisher()._with_account("@mychannel") == (None, "@mychannel")


class _Resp:
    status = 200

    async def json(self, content_type=None):
        return {"id": "post-1"}

    async def text(self):
        return ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self, calls):
        self.calls = calls

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return _Resp()

    async def close(self):
        pass


def test_publishing_as_the_second_account_uses_its_token_and_page(store, monkeypatch):
    accounts.save_account("facebook", "P1", "Shop", "t1", config={"page_id": "P1"})
    accounts.save_account("facebook", "P2", "Blog", "t2", config={"page_id": "P2"})
    pub = publishers.FacebookPagePublisher()
    calls: list = []

    async def session():
        return _Session(calls)

    monkeypatch.setattr(pub, "_session", session)
    post = RenderedPost(text="hello", media=[], link=None)

    r2 = asyncio.run(pub.publish("@P2", post))
    r1 = asyncio.run(pub.publish("", post))
    assert r2.ok and r1.ok
    assert calls[0][0].endswith("/P2/feed") and calls[0][1]["data"]["access_token"] == "t2"
    assert calls[1][0].endswith("/P1/feed") and calls[1][1]["data"]["access_token"] == "t1"


def test_an_unknown_account_is_not_connected(store):
    accounts.save_account("facebook", "P1", "Shop", "t1", config={"page_id": "P1"})
    r = asyncio.run(publishers.FacebookPagePublisher().publish("@nope", RenderedPost(text="x", media=[], link=None)))
    assert not r.ok and r.requires_auth


def test_refresh_renews_the_account_it_was_asked_for(store, monkeypatch):
    secrets, _ = store
    accounts.save_account("youtube", "UC1", "Studio", "tok1", refresh="rt1")
    accounts.save_account("youtube", "UC2", "Personal", "tok2", refresh="rt2")
    monkeypatch.setattr(oauth, "_require_creds", lambda *p: ["cid", "secret"])
    seen = {}

    def post_form(url, form, basic=None):
        seen["rt"] = form["refresh_token"]
        return {"access_token": "fresh2"}

    monkeypatch.setattr(oauth, "_post_form", post_form)
    oauth.refresh_provider("youtube", "UC2")
    assert seen["rt"] == "rt2"
    assert secrets["youtube@UC2"] == "fresh2" and secrets["youtube"] == "tok1"
