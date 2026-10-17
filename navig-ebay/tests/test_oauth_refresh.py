"""OAuth token exchange/refresh and expiry logic (mocked HTTP)."""

from __future__ import annotations

import time

import pytest

from navig_ebay.engine import oauth_ebay
from navig_ebay.engine.models import EbayAuthError, TokenSet, endpoints_for


class _FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def test_tokenset_expiry_buffer():
    # Expires in 1 minute → within the 5-minute buffer → considered expired.
    soon = TokenSet(access="a", expires=int((time.time() + 60) * 1000))
    assert soon.is_expired
    later = TokenSet(access="a", expires=int((time.time() + 3600) * 1000))
    assert not later.is_expired


def test_refresh_keeps_existing_refresh_token_when_not_returned(monkeypatch):
    def fake_post(url, data, auth, headers, timeout):
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == "RT"
        # eBay does not return a new refresh token on refresh.
        return _FakeResp(200, {"access_token": "NEW_ACCESS", "expires_in": 7200})

    monkeypatch.setattr(oauth_ebay.requests, "post", fake_post)
    ts = oauth_ebay.refresh_user_token(
        environment="sandbox", client_id="cid", client_secret="sec", refresh_token="RT"
    )
    assert ts.access == "NEW_ACCESS"
    assert ts.refresh == "RT"  # preserved
    assert not ts.is_expired


def test_exchange_code_builds_tokenset(monkeypatch):
    def fake_post(url, data, auth, headers, timeout):
        assert data["grant_type"] == "authorization_code"
        assert data["redirect_uri"] == "MyRuName"
        assert url == endpoints_for("sandbox")["token"]
        return _FakeResp(200, {"access_token": "AC", "refresh_token": "RF", "expires_in": 7200})

    monkeypatch.setattr(oauth_ebay.requests, "post", fake_post)
    ts = oauth_ebay.exchange_code(
        environment="sandbox", client_id="cid", client_secret="sec", code="CODE", ru_name="MyRuName"
    )
    assert ts.access == "AC"
    assert ts.refresh == "RF"


def test_refresh_without_token_raises():
    with pytest.raises(EbayAuthError):
        oauth_ebay.refresh_user_token(
            environment="sandbox", client_id="c", client_secret="s", refresh_token=""
        )


def test_token_endpoint_error_raises(monkeypatch):
    monkeypatch.setattr(
        oauth_ebay.requests, "post", lambda *a, **k: _FakeResp(400, {"error": "invalid_grant"})
    )
    with pytest.raises(EbayAuthError):
        oauth_ebay.fetch_app_token(environment="sandbox", client_id="c", client_secret="s")


def test_build_authorize_url_has_required_params():
    url = oauth_ebay.build_authorize_url(
        client_id="cid", ru_name="MyRuName", environment="sandbox", state="xyz"
    )
    assert url.startswith(endpoints_for("sandbox")["authorize"])
    assert "client_id=cid" in url
    assert "redirect_uri=MyRuName" in url
    assert "state=xyz" in url
    assert "response_type=code" in url
