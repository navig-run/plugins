"""AI design-edit facet — the primitive + the ``navig design edit`` CLI.

Core's ``get_ai_client`` is monkeypatched with a fake whose ``complete`` returns
canned markup, so nothing hits a provider. Asserts fence stripping, input
validation, provider-failure surfacing, and that the CLI emits parseable JSON.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from navig_text.design import _clean, design_edit


class _FakeClient:
    provider = "fake"
    model = "fake-model"

    def is_available(self) -> bool:
        return True

    async def complete(self, prompt, system_prompt=None, **_kw) -> str:
        # Model wraps its answer in a fence (common) — we must strip it.
        return '```html\n<button style="background:#000;color:#fff">Buy now</button>\n```'


@pytest.fixture(autouse=True)
def _patch_client(monkeypatch):
    import navig.agent.ai_client as aic

    monkeypatch.setattr(aic, "get_ai_client", lambda: _FakeClient())


def test_clean_unwraps_fences():
    assert _clean("```html\n<b>x</b>\n```") == "<b>x</b>"
    assert _clean("```\n<b>x</b>\n```") == "<b>x</b>"
    assert _clean("<b>x</b>") == "<b>x</b>"


def test_design_edit_returns_clean_html():
    r = asyncio.run(design_edit("<button>Buy</button>", "make it dark", selector="button"))
    assert r.html.startswith("<button")
    assert "```" not in r.html
    assert r.model == "fake-model"
    assert r.instruction == "make it dark"


def test_requires_html_and_instruction():
    with pytest.raises(ValueError):
        asyncio.run(design_edit("", "do x"))
    with pytest.raises(ValueError):
        asyncio.run(design_edit("<div></div>", "   "))


def test_empty_completion_raises(monkeypatch):
    import navig.agent.ai_client as aic

    class _Empty(_FakeClient):
        async def complete(self, *a, **k):
            return "   "

    monkeypatch.setattr(aic, "get_ai_client", lambda: _Empty())
    with pytest.raises(RuntimeError):
        asyncio.run(design_edit("<div>x</div>", "do x"))


def test_prose_completion_raises(monkeypatch):
    import navig.agent.ai_client as aic

    class _Prose(_FakeClient):
        async def complete(self, *a, **k):
            return "Sure! Here is the revised element you asked for."

    monkeypatch.setattr(aic, "get_ai_client", lambda: _Prose())
    with pytest.raises(RuntimeError):
        asyncio.run(design_edit("<div>x</div>", "do x"))


def test_cli_edit_b64_emits_json():
    from typer.testing import CliRunner

    from navig_text.commands.design import design_app

    payload = base64.b64encode(
        json.dumps({"html": "<button>Buy</button>", "prompt": "make it dark"}).encode()
    ).decode()
    res = CliRunner().invoke(design_app, ["edit", "--b64", payload])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output.strip().splitlines()[-1])
    assert data["ok"] is True
    assert data["html"].startswith("<button")


def test_cli_edit_missing_args_fails_as_json():
    from typer.testing import CliRunner

    from navig_text.commands.design import design_app

    res = CliRunner().invoke(design_app, ["edit", "--prompt", "dark"])  # no html
    assert res.exit_code == 2
    data = json.loads(res.output.strip().splitlines()[-1])
    assert data["ok"] is False
    assert "required" in data["error"]
