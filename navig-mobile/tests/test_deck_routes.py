"""Gateway route handler tests — /api/deck/mobile/* (called directly, no server)."""

from __future__ import annotations

import asyncio
import json

from navig_mobile.deck_routes import mobile as routes
from navig_mobile.engine.base import AppInfo, DeviceInfo, Platform


class FakeReq:
    def __init__(self, match_info=None, query=None):
        self.match_info = match_info or {}
        self.query = query or {}


def _run(coro):
    return asyncio.run(coro)


def _json(resp):
    return json.loads(resp.body)


def _patch(monkeypatch, android=(), ios=(), get_device=None):
    import navig_mobile.engine.android.device as adev
    import navig_mobile.engine.ios.device as idev

    monkeypatch.setattr(adev, "ensure_available", lambda: None)
    monkeypatch.setattr(idev, "ensure_available", lambda: None)
    monkeypatch.setattr(adev, "list_devices", lambda: list(android))
    monkeypatch.setattr(idev, "list_devices", lambda: list(ios))
    if get_device:
        monkeypatch.setattr(adev, "get_device", get_device)
        monkeypatch.setattr(idev, "get_device", get_device)


def test_devices_route(monkeypatch):
    _patch(monkeypatch, android=[DeviceInfo(udid="A1", platform=Platform.ANDROID, name="Pixel")])
    resp = _run(routes.handle_devices(FakeReq()))
    assert resp.status == 200
    data = _json(resp)
    assert data["ok"] and data["data"][0]["udid"] == "A1"


def test_doctor_route():
    d = _json(_run(routes.handle_doctor(FakeReq())))
    assert d["ok"] and "android_ready" in d["data"] and isinstance(d["data"]["tools"], list)


def test_device_info_route(monkeypatch):
    class FakeDev:
        def info(self):
            return DeviceInfo(udid="A1", platform=Platform.ANDROID, name="Pixel", battery=90)

    _patch(monkeypatch, android=[DeviceInfo(udid="A1", platform=Platform.ANDROID)],
           get_device=lambda u: FakeDev())
    d = _json(_run(routes.handle_device_info(FakeReq(match_info={"udid": "A1"}))))
    assert d["ok"] and d["data"]["battery"] == 90


def test_device_info_not_found(monkeypatch):
    _patch(monkeypatch)  # nothing connected
    resp = _run(routes.handle_device_info(FakeReq(match_info={"udid": "NOPE"})))
    assert resp.status == 404 and _json(resp)["ok"] is False


def test_device_apps_route(monkeypatch):
    class FakeDev:
        def apps(self, *, system=False):
            return [AppInfo(app_id="com.x", name="X")]

    _patch(monkeypatch, android=[DeviceInfo(udid="A1", platform=Platform.ANDROID)],
           get_device=lambda u: FakeDev())
    d = _json(_run(routes.handle_device_apps(FakeReq(match_info={"udid": "A1"}))))
    assert d["ok"] and d["data"][0]["app_id"] == "com.x"


def test_backups_and_cases_routes():
    from navig_mobile.store import get_store

    get_store().record_backup(udid="A1", path="/b.ab", size_bytes=1)
    assert _json(_run(routes.handle_backups(FakeReq())))["data"][0]["path"] == "/b.ab"
    cases = _json(_run(routes.handle_cases(FakeReq())))
    assert cases["ok"] and cases["data"] == []


def test_screenshot_route(monkeypatch):
    from pathlib import Path

    class FakeDev:
        udid = "A1"

        def screenshot(self, dest):
            Path(dest).write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
            return dest

    _patch(monkeypatch, android=[DeviceInfo(udid="A1", platform=Platform.ANDROID)],
           get_device=lambda u: FakeDev())
    d = _json(_run(routes.handle_screenshot(FakeReq(match_info={"udid": "A1"}))))
    assert d["ok"] and d["data"]["bytes"] > 0
    assert d["data"]["data_uri"].startswith("data:image/png;base64,")


def test_register_mounts_routes():
    from aiohttp import web

    app = web.Application()
    routes.register(app)
    assert len(list(app.router.routes())) >= 7
