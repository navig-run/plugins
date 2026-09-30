"""Unit tests for the social OAuth pure helpers (no network, no vault).

Covers the parse-back parser (the manual-mode security surface) and the
provider/manual-only registries. The HTTP flows themselves need live provider
endpoints and are exercised via ``navig social status --check``.
"""
from __future__ import annotations

import sys

import pytest

from navig_social.social import oauth


def test_parse_manual_reply_full_url():
    url = "https://navig-lighthouse.example/oauth/callback?code=ABC123&state=xyz"
    assert oauth.parse_manual_reply(url, expected_state="xyz") == "ABC123"


def test_parse_manual_reply_bare_code():
    assert oauth.parse_manual_reply("ABC123") == "ABC123"


def test_parse_manual_reply_strips_threads_fragment():
    assert oauth.parse_manual_reply("code=ABC123#_") == "ABC123"
    url = "https://x/cb?code=ABC123#_"
    assert oauth.parse_manual_reply(url) == "ABC123"


def test_parse_manual_reply_state_mismatch_rejected():
    url = "https://x/cb?code=ABC&state=evil"
    with pytest.raises(oauth.SocialOAuthError):
        oauth.parse_manual_reply(url, expected_state="expected")


def test_parse_manual_reply_error_param_raised():
    url = "https://x/cb?error=access_denied&error_description=nope"
    with pytest.raises(oauth.SocialOAuthError):
        oauth.parse_manual_reply(url)


def test_parse_manual_reply_missing_code():
    with pytest.raises(oauth.SocialOAuthError):
        oauth.parse_manual_reply("https://x/cb?state=only")


def test_parse_manual_reply_empty():
    with pytest.raises(oauth.SocialOAuthError):
        oauth.parse_manual_reply("   ")


def test_every_connector_is_known_and_threads_is_manual_only():
    for name in ("facebook", "instagram", "threads", "linkedin", "youtube", "pinterest", "devto"):
        assert name in oauth.CONNECTORS
    # Threads rejects http:// redirects, so it must force paste-back mode.
    assert "threads" in oauth.MANUAL_ONLY


def test_redirect_uri_is_the_shared_loopback():
    assert oauth.REDIRECT_URI == f"http://localhost:{oauth.CALLBACK_PORT}/oauth/callback"


def test_build_auth_url_encodes_params():
    url = oauth.build_auth_url("https://x/auth", {"client_id": "a b", "scope": "x,y"})
    assert url.startswith("https://x/auth?")
    assert "client_id=a+b" in url
    assert "scope=x%2Cy" in url


def test_set_config_deep_sets_leaf_without_clobbering_siblings(monkeypatch):
    """Regression: set_config once called a non-existent ConfigManager.set() and,
    had it used update_global_config, would have replaced the whole `adapters`
    subtree — wiping a sibling provider's config. It must deep-set only the leaf.

    Inside navig that is ConfigManager.set_global (refresh → deep-set → save)."""
    import types

    saved = {}

    class _CM:
        def set_global(self, dotted, value):
            saved[dotted] = value

    fake = types.ModuleType("navig.config")
    fake.get_config_manager = lambda: _CM()
    monkeypatch.setitem(sys.modules, "navig.config", fake)

    oauth.set_config("linkedin", "author_urn", "urn:li:person:ABC")
    assert saved == {"adapters.social.linkedin.author_urn": "urn:li:person:ABC"}


def test_set_config_on_its_own_keeps_sibling_providers(monkeypatch, tmp_path):
    """Without navig the same leaf-only write lands in the same config.yaml."""
    pytest.importorskip("yaml")
    from navig_sdk import files

    monkeypatch.setitem(sys.modules, "navig.config", None)
    monkeypatch.setenv("NAVIG_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "adapters:\n  social:\n    facebook:\n      page_id: PAGE123\n", encoding="utf-8"
    )
    oauth.set_config("linkedin", "author_urn", "urn:li:person:ABC")
    social = files.safe_load_yaml(tmp_path / "config.yaml")["adapters"]["social"]
    assert social["linkedin"]["author_urn"] == "urn:li:person:ABC"   # leaf written
    assert social["facebook"]["page_id"] == "PAGE123"                # sibling intact
