"""A browser that is not running is an ordinary state, not a crash report.

Nine CDP verbs called ``_bridge`` directly, so a dead port escaped as the bridge's
``RuntimeError`` and the CLI's crash handler printed ``💥 unexpected error`` and wrote a
crash file for what the user can fix in one line (start a browser). The verbs that went
through ``_try_bridge`` already returned ``{"ok": False, "error": "no CDP target …"}``.

Every verb is covered here, not just ``eval``: they share one bridge, so a fix that
protected one of them would be a guard over a path rather than over the surface.
"""

from __future__ import annotations

import asyncio

import pytest

from navig_browser import cdp_actions

PORT = 9299  # nothing ever listens here

# (function, kwargs) — one per verb that fetched the bridge itself.
CASES = [
    ("snapshot", {}),
    ("scroll", {"delta_y": 5.0}),
    ("move", {"x": 1.0, "y": 1.0}),
    ("eval_js", {"expression": "1 + 1"}),
    ("navigate", {"url": "about:blank"}),
    ("switch", {}),
    ("login", {"domain": "example.com"}),
    ("gmail_compose", {"to": "someone@example.com"}),
    ("inject_script", {"script": "console.log('hi')"}),
]


def _dead_bridge(*_args, **_kwargs):
    raise RuntimeError(
        f"[CDPBridge] Could not connect to Chrome at http://localhost:{PORT}. "
        "Make sure Chrome is running with --remote-debugging-port."
    )


def _call(name: str, kwargs: dict) -> dict:
    return asyncio.run(getattr(cdp_actions, name)(port=PORT, **kwargs))


@pytest.mark.parametrize(("name", "kwargs"), CASES, ids=[c[0] for c in CASES])
def test_a_dead_browser_is_a_clean_error_not_a_crash(monkeypatch, name, kwargs):
    """The attach failure is returned, never raised — raising reaches the crash handler."""
    async def boom(*_a, **_k):
        raise _dead_bridge()

    monkeypatch.setattr(cdp_actions, "_bridge", boom)
    res = _call(name, kwargs)
    assert res["ok"] is False, res
    assert f"no CDP target on port {PORT}" in res["error"], res
    assert "navig cdp new" in res["error"], res


def test_a_real_dead_port_says_the_same_thing():
    """The real stack, not a mock: a connection refused on a dead port."""
    res = asyncio.run(cdp_actions.eval_js(port=PORT, expression="1"))
    assert res["ok"] is False, res
    assert f"no CDP target on port {PORT}" in res["error"], res


def test_the_refusal_is_one_shared_shape():
    assert cdp_actions._no_target(PORT) == {
        "ok": False,
        "error": f"no CDP target on port {PORT} — start one with `navig cdp new`",
    }
