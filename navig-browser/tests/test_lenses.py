"""Lenses in navig-browser: every controller registers them, and nothing breaks without them."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1] / "navig_browser"

SRC = (
    "// ==UserScript==\n"
    "// @name Marker\n"
    "// @match *://lens.test/*\n"
    "// @run-at document-start\n"
    "// ==/UserScript==\n"
    "window.__lens = 'on';\n"  # document-start: no <html> element exists yet
)


class FakeContext:
    def __init__(self, refuse: bool = False) -> None:
        self.scripts: list[str] = []
        self.refuse = refuse

    async def add_init_script(self, code: str) -> None:
        if self.refuse:
            raise RuntimeError("target closed")
        self.scripts.append(code)


@pytest.fixture
def store(tmp_path, monkeypatch):
    pytest.importorskip("navig.lenses")
    monkeypatch.setenv("NAVIG_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("NAVIG_LENSES", raising=False)
    from navig.lenses import LensStore, lenses_dir

    assert lenses_dir() == tmp_path / "cfg" / "lenses"  # never the operator's real store
    monkeypatch.setattr("navig.license.entitlement.live_caps_and_rank", lambda: ([], 0))
    s = LensStore()
    s.save(SRC)
    return s


def test_apply_registers_every_enabled_lens(store) -> None:
    from navig_browser.lenses import apply_lenses

    ctx = FakeContext()
    assert asyncio.run(apply_lenses(ctx)) == 1
    assert "navigLensRuntime" in ctx.scripts[0] and "lens.test" in ctx.scripts[0]


def test_disabled_lenses_and_the_opt_out_register_nothing(store, monkeypatch) -> None:
    from navig_browser.lenses import apply_lenses

    monkeypatch.setenv("NAVIG_LENSES", "0")
    assert asyncio.run(apply_lenses(FakeContext())) == 0
    monkeypatch.delenv("NAVIG_LENSES")
    (rec,) = store.records()
    store.set_enabled(rec.id, False)
    assert asyncio.run(apply_lenses(FakeContext())) == 0


def test_without_core_lenses_the_browser_still_starts(monkeypatch) -> None:
    """navig-browser standalone, or against a core that predates Lenses."""
    monkeypatch.setitem(sys.modules, "navig.lenses", None)  # import → ImportError
    from navig_browser.lenses import apply_lenses, compile_userscript

    assert asyncio.run(apply_lenses(FakeContext())) == 0
    assert compile_userscript(SRC) is None  # caller injects the raw file, as before


def test_a_broken_store_or_refusing_context_never_raises(store, monkeypatch) -> None:
    from navig_browser.lenses import apply_lenses

    assert asyncio.run(apply_lenses(FakeContext(refuse=True))) == 0

    def boom(self, host, **_k):
        raise PermissionError("index locked")

    monkeypatch.setattr("navig.lenses.LensStore.compiled", boom)
    assert asyncio.run(apply_lenses(FakeContext())) == 0


def test_compile_userscript_honours_the_header() -> None:
    pytest.importorskip("navig.lenses")
    from navig_browser.lenses import compile_userscript

    assert compile_userscript("console.log(1)") is None  # plain JS stays plain
    code = compile_userscript(SRC)
    assert code is not None and "navigLensRuntime" in code
    with pytest.raises(ValueError, match="@match"):
        compile_userscript(SRC.replace("*://lens.test/*", "nope"))


# Every place that creates or attaches a context must register the Lenses — a controller
# that forgets is a browser where the user's Lenses silently never run.
STARTS = {
    "controller.py": "BrowserController",
    "cdp_bridge.py": "CDPBridge",
    "stealth.py": "StealthController",
    "system_chrome.py": "SystemChromeController",
    "firefox.py": "FirefoxController",
}


@pytest.mark.parametrize(("module", "cls"), sorted(STARTS.items()))
def test_every_controller_start_applies_lenses(module: str, cls: str) -> None:
    tree = ast.parse((PKG / module).read_text(encoding="utf-8"))
    klass = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    start = next(
        n for n in klass.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "start"
    )
    calls = [
        n for n in ast.walk(start)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "apply_lenses"
    ]
    assert calls, f"{cls}.start() never calls apply_lenses()"


def test_lenses_run_in_a_real_page(store) -> None:
    if importlib.util.find_spec("playwright") is None:
        pytest.skip("playwright not installed")
    from playwright.async_api import async_playwright

    from navig_browser.lenses import apply_lenses

    async def go() -> tuple[str | None, str | None]:
        async with async_playwright() as pw:
            browser = None
            for channel in (None, "chrome", "msedge"):
                try:
                    browser = await pw.chromium.launch(headless=True, channel=channel)
                    break
                except Exception:  # noqa: BLE001 — that binary isn't here
                    continue
            if browser is None:
                pytest.skip("no chromium available")
            try:
                ctx = await browser.new_context()
                await ctx.route(
                    "**/*",
                    lambda r: r.fulfill(status=200, content_type="text/html", body="<p>x</p>"),
                )
                assert await apply_lenses(ctx) == 1
                page = await ctx.new_page()
                await page.goto("https://lens.test/")
                on = await page.evaluate("window.__lens ?? null")
                await page.goto("https://other.test/")
                off = await page.evaluate("window.__lens ?? null")
                return on, off
            finally:
                await browser.close()

    assert asyncio.run(go()) == ("on", None)
