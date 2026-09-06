"""Unit tests for navig_social.stats — the universal public-stat engine behind
`navig social stats`. Pure parsers + routing only; no network, no browser.

Ported from the retired navig-presence plugin (the space-bound registry layer was
dropped); plus a test for the new universal crawl_handles() batch entry point.
"""
from __future__ import annotations

from navig_social import stats


def test_parse_github():
    payload = '{"login":"torvalds","followers":68,"public_repos":7,"name":"x"}'
    assert stats.parse_github(payload) == {"followers": 68, "public_repos": 7}


def test_parse_telegram_plain():
    assert stats.parse_telegram_subscribers("<div>1234 subscribers</div>") == 1234


def test_parse_telegram_grouped_and_nbsp():
    assert stats.parse_telegram_subscribers("12 345 subscribers") == 12345
    assert stats.parse_telegram_subscribers("<b>2 345</b> members") == 2345


def test_parse_telegram_absent():
    assert stats.parse_telegram_subscribers("<html>no channel here</html>") is None


def test_humanish_to_int():
    assert stats._humanish_to_int("1.2K") == 1200
    assert stats._humanish_to_int("3,400") == 3400
    assert stats._humanish_to_int("2 345") == 2345
    assert stats._humanish_to_int("5M") == 5_000_000
    assert stats._humanish_to_int("") is None
    assert stats._humanish_to_int(None) is None


def test_platform_capability():
    assert stats.platform_capability("github") == "public"
    assert stats.platform_capability("telegram") == "public"
    assert stats.platform_capability("kick") == "public"
    assert stats.platform_capability("soundcloud") == "public"
    assert stats.platform_capability("youtube") == "api"
    assert stats.platform_capability("vk") == "api"
    assert stats.platform_capability("facebook") == "api"
    assert stats.platform_capability("twitch") == "api"
    assert stats.platform_capability("instagram") == "api"
    assert stats.platform_capability("tiktok") == "cdp-login"
    assert stats.platform_capability("myspace") == "unsupported"


def test_parse_vk_group_and_user():
    grp = '{"response":{"groups":[{"id":1,"members_count":4321}],"profiles":[]}}'
    assert stats.parse_vk(grp) == (4321, "group")
    usr = '{"response":[{"id":7,"followers_count":88}]}'
    assert stats.parse_vk(usr) == (88, "user")
    assert stats.parse_vk('{"error":{"error_code":100}}') == (None, "")


def test_parse_facebook_graph():
    assert stats.parse_facebook_graph('{"followers_count":1200,"fan_count":1150,"id":"9"}') == 1200
    assert stats.parse_facebook_graph('{"fan_count":50,"id":"9"}') == 50
    assert stats.parse_facebook_graph('{"error":{"message":"bad token"}}') is None


def test_parse_kick():
    assert stats.parse_kick('{"slug":"x","followers_count":1092533,"is_banned":false}') == 1092533
    assert stats.parse_kick('{"followersCount":42}') == 42
    assert stats.parse_kick('{"slug":"x"}') is None


def test_parse_soundcloud_followers():
    assert stats.parse_soundcloud_followers('...,"followers_count": 485,"kind":"user"...') == 485
    assert stats.parse_soundcloud_followers("<html>nothing</html>") is None


def test_parse_youtube_api():
    payload = ('{"items":[{"statistics":{"subscriberCount":"1234",'
               '"videoCount":"56","viewCount":"78901","hiddenSubscriberCount":false}}]}')
    assert stats.parse_youtube_api(payload) == {
        "followers": 1234, "videos": 56, "views": 78901, "hidden": False}


def test_parse_youtube_api_hidden_and_empty():
    hidden = '{"items":[{"statistics":{"hiddenSubscriberCount":true,"videoCount":"3"}}]}'
    p = stats.parse_youtube_api(hidden)
    assert p["followers"] is None and p["hidden"] is True
    assert stats.parse_youtube_api('{"items":[]}') == {"followers": None, "hidden": False}


def test_yt_key_shape():
    assert stats._YT_KEY_RE.match("AIzaSyA2GTLJEG7FtYhNaoRGouE5pfQyBsKOMEY")
    assert not stats._YT_KEY_RE.match("123456.apps.googleusercontent.com")
    assert not stats._YT_KEY_RE.match("sk-not-a-google-key")


def test_youtube_api_key_env(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "AIzaTESTKEY")
    assert stats.youtube_api_key() == "AIzaTESTKEY"


def test_parse_port():
    assert stats._parse_port("Launched at http://127.0.0.1:9222/json") == 9222
    assert stats._parse_port('{"port": 54321}') == 54321
    assert stats._parse_port("no port here") is None


def test_crawl_account_no_handle():
    r = stats.crawl_account({"platform": "linkedin", "handle": "TODO", "url": "x"})
    assert r.status == "no-handle"


def test_crawl_account_only_public_defers_cdp():
    r = stats.crawl_account(
        {"platform": "tiktok", "handle": "@x", "url": "https://tiktok.com/@x"}, allow_cdp=False)
    assert r.status in ("needs-login", "needs-cdp")


def test_crawl_account_youtube_no_key_only_public(monkeypatch):
    monkeypatch.setattr(stats, "youtube_api_key", lambda: None)
    r = stats.crawl_account(
        {"platform": "youtube", "handle": "@x", "url": "https://youtube.com/@x"}, allow_cdp=False)
    assert r.status in ("needs-token", "needs-cdp")


def test_vk_facebook_no_token_only_public(monkeypatch):
    monkeypatch.setattr(stats, "vk_token", lambda: None)
    monkeypatch.setattr(stats, "facebook_token", lambda: None)
    assert stats.crawl_account({"platform": "vk", "handle": "x"}, allow_cdp=False).status in (
        "needs-token", "needs-login")
    assert stats.crawl_account({"platform": "facebook", "handle": "x"}, allow_cdp=False).status in (
        "needs-token", "needs-login")


# --- the new universal batch entry point (no space) ---

def test_crawl_handles_batch_no_cdp(monkeypatch):
    # github is public → stub the fetcher offline (patch the DICT entry, since
    # PUBLIC_FETCHERS captured the function ref at import); tiktok defers without CDP.
    monkeypatch.setitem(
        stats.PUBLIC_FETCHERS, "github",
        lambda handle: stats.StatResult(platform="github", handle=handle, followers=68, status="ok"),
    )
    results = stats.crawl_handles(
        [("github", "torvalds"), ("tiktok", "@x")], allow_cdp=False)
    assert len(results) == 2
    assert results[0].platform == "github" and results[0].followers == 68
    assert results[1].status in ("needs-login", "needs-cdp")


def test_crawl_handles_lowercases_platform(monkeypatch):
    monkeypatch.setitem(
        stats.PUBLIC_FETCHERS, "github",
        lambda handle: stats.StatResult(platform="github", handle=handle, followers=1, status="ok"),
    )
    # Uppercase platform must still route (capability lookups are lower-cased).
    r = stats.crawl_handles([("GitHub", "torvalds")], allow_cdp=False)
    assert r[0].status == "ok"
