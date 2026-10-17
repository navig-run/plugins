"""`navig cdp new -e` reports an extension as loaded only when the browser says so.

Branded Chrome 137+ accepts --load-extension and loads nothing; the command used to print
`loaded_extension` from the flag alone. These drive `new()` with the launch and the
browser's answer faked, so they run without a browser.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from navig_browser import cdp_actions as A


class _Target:
    def to_dict(self) -> dict:
        return {"browser": "fake"}


@pytest.fixture
def ext(tmp_path: Path) -> Path:
    d = tmp_path / "my-extension"
    d.mkdir()
    (d / "manifest.json").write_text('{"manifest_version": 3}', encoding="utf-8")
    return d


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    from navig_browser import targets as t

    state: dict = {"stopped": []}
    monkeypatch.setattr(t, "find_free_port", lambda *a, **k: 9555)
    monkeypatch.setattr(t, "new_session_profile_dir", lambda profile=None: str(tmp_path / "profile"))
    monkeypatch.setattr(t, "launch_with_cdp", lambda *a, **k: _Target())

    def stop(port: int) -> dict:
        state["stopped"].append(port)
        return {"ok": True, "closed": True, "port": port}

    monkeypatch.setattr(t, "stop_launched", stop)
    return state


def _browser_lists(monkeypatch: pytest.MonkeyPatch, listing: list[dict] | None) -> None:
    async def fake(port: int, *, timeout: float):  # noqa: ARG001
        return listing

    monkeypatch.setattr(A, "_installed_extensions", fake)


def test_a_browser_that_loaded_nothing_fails_honestly_and_is_closed(monkeypatch, ext, launched):
    _browser_lists(monkeypatch, [])  # what branded Chrome 151 answers
    out = A.new(load_extension=str(ext), headless=True)
    assert out["ok"] is False
    assert "did not load" in out["error"] and "Chromium" in out["error"]
    assert "loaded_extension" not in out
    assert launched["stopped"] == [9555]  # our own throwaway session — no browser left behind


def test_a_loaded_extension_is_reported_with_its_name(monkeypatch, ext, launched):
    _browser_lists(monkeypatch, [{"name": "NAVIG Dock", "path": str(ext)}])
    out = A.new(load_extension=str(ext), headless=True)
    assert out["ok"] is True
    assert out["loaded_extension"] == str(ext)
    assert out["extension_check"]["names"] == ["NAVIG Dock"]
    assert launched["stopped"] == []


def test_path_comparison_ignores_case_and_separators_on_windows(monkeypatch, ext, launched):
    import os

    if os.name != "nt":
        pytest.skip("case-insensitive paths are a Windows property")
    _browser_lists(monkeypatch, [{"name": "x", "path": str(ext).upper().replace("\\", "/")}])
    assert A.new(load_extension=str(ext), headless=True)["ok"] is True


def test_cannot_check_is_not_reported_as_loaded(monkeypatch, ext, launched):
    _browser_lists(monkeypatch, None)  # no developerPrivate / probe timed out
    out = A.new(load_extension=str(ext), headless=True)
    assert out["ok"] is True  # the session is real and usable
    assert out["extension_check"]["verified"] is None
    assert "loaded_extension" not in out
    assert launched["stopped"] == []


def test_without_an_extension_nothing_is_probed(monkeypatch, launched):
    def boom(*a, **k):
        raise AssertionError("probed a browser that was not asked to load an extension")

    monkeypatch.setattr(A, "verify_extensions", boom)
    out = A.new(headless=True)
    assert out["ok"] is True and "extension_check" not in out


def test_launch_reports_but_never_closes_the_operators_browser(monkeypatch, ext):
    from navig_browser import targets as t

    _browser_lists(monkeypatch, [])
    monkeypatch.setattr(t, "probe_port", lambda *a, **k: None)
    monkeypatch.setattr(t, "launch_with_cdp", lambda *a, **k: _Target())
    monkeypatch.setattr(t, "stop_launched", lambda port: pytest.fail("launch must not close the browser"))
    out = A.launch("chrome", port=9556, load_extension=str(ext), headless=True)
    assert out["ok"] is False and "did not load" in out["error"]
