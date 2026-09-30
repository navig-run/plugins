"""Config manager round-trip + endpoints/error helpers."""

from __future__ import annotations

import pytest

from navig_ebay.engine.config import EbayConfig, EbayConfigManager
from navig_ebay.engine.models import EbayApiError, EbayConfigError, endpoints_for
from navig_ebay.engine.api import _describe_error


def test_config_roundtrip(tmp_path):
    path = tmp_path / "cfg.yaml"
    mgr = EbayConfigManager(path=path)
    # default when absent
    assert mgr.load().environment == "sandbox"
    mgr.update(environment="production", ru_name="MyRuName", default_location_key="home-1")
    reloaded = EbayConfigManager(path=path).load()
    assert reloaded.environment == "production"
    assert reloaded.ru_name == "MyRuName"
    assert reloaded.default_location_key == "home-1"


def test_config_update_unknown_key_goes_to_extra(tmp_path):
    mgr = EbayConfigManager(path=tmp_path / "cfg.yaml")
    cfg = mgr.update(some_flag="on")
    assert cfg.extra["some_flag"] == "on"
    assert EbayConfigManager(path=mgr.path).load().extra["some_flag"] == "on"


def test_from_dict_ignores_unknown_fields():
    cfg = EbayConfig.from_dict({"environment": "sandbox", "bogus": 1})
    assert cfg.environment == "sandbox"
    assert not hasattr(cfg, "bogus")


def test_endpoints_known_and_unknown():
    assert "authorize" in endpoints_for("sandbox")
    assert endpoints_for("production")["api"] == "https://api.ebay.com"
    with pytest.raises(EbayConfigError):
        endpoints_for("staging")


def test_describe_error_extracts_ebay_message():
    payload = {"errors": [{"errorId": 25001, "message": "System error", "longMessage": "boom"}]}
    msg = _describe_error(payload)
    assert "System error" in msg
    assert "25001" in msg


def test_ebay_api_error_carries_status():
    e = EbayApiError(404, "not found", {"errors": []})
    assert e.status == 404
    assert "404" in str(e)
