"""Media generation deck routes — registration + module-gating coverage.

Verifies the generation route surface mounts and that EVERY media route is
wrapped by ``requires_module("generate")`` (so toggling the free module off makes
them all 403 — the same live-remove Studio gets).
"""

from __future__ import annotations

import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402

from navig_generate.deck_routes import generation  # noqa: E402

EXPECTED = {
    ("POST", "/api/deck/media/generate"),
    ("POST", "/api/deck/media/reroll/{id}"),
    ("GET", "/api/deck/media/jobs/{id}"),
    ("POST", "/api/deck/media/{id}/keep"),
    ("POST", "/api/deck/media/{id}/reject"),
    ("POST", "/api/deck/media/{id}/edit"),
    ("POST", "/api/deck/media/{id}/remove-bg"),
    ("POST", "/api/deck/media/{id}/redesign"),
    ("POST", "/api/deck/media/{id}/process"),
    ("POST", "/api/deck/media/{id}/license"),
    ("POST", "/api/deck/media/ingest"),
    ("GET", "/api/deck/media/contact-sheet"),
    ("GET", "/api/deck/media/palette"),
    ("GET", "/api/deck/media/history"),
    ("GET", "/api/deck/media/context"),
    ("GET", "/api/deck/media/file/{id}/raw"),
    ("POST", "/api/deck/media/reference"),
    ("GET", "/api/deck/media/reference/{id}/raw"),
}


def _routes(app: web.Application) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        path = info.get("path") or info.get("formatter") or ""
        out.add((route.method, path))
    return out


def test_all_generation_routes_register():
    app = web.Application()
    generation.register(app)
    assert EXPECTED <= _routes(app)


def test_prune_finished_jobs_caps_registry_and_keeps_running():
    """_JOBS must not grow unboundedly: oldest FINISHED jobs are dropped past the
    cap; running jobs are never pruned regardless of age."""
    generation._JOBS.clear()
    generation._JOBS["oldest-running"] = {"state": "running"}
    for i in range(60):
        generation._JOBS[f"done-{i}"] = {"state": "done" if i % 2 else "failed"}
    generation._prune_finished_jobs(limit=50)
    assert "oldest-running" in generation._JOBS  # running survives
    finished = [j for j in generation._JOBS.values() if j["state"] in ("done", "failed")]
    assert len(finished) == 50
    # Oldest finished were dropped, newest retained.
    assert "done-0" not in generation._JOBS and "done-59" in generation._JOBS
    generation._JOBS.clear()


def test_every_media_route_is_module_gated():
    app = web.Application()
    generation.register(app)
    media_routes = [r for r in app.router.routes()
                    if (r.resource.get_info().get("path")
                        or r.resource.get_info().get("formatter") or "").startswith("/api/deck/media")]
    assert media_routes, "no media routes registered"
    for r in media_routes:
        # requires_module tags each wrapped handler for introspection.
        assert getattr(r.handler, "__navig_required_module__", None) == "generate", (
            f"{r.method} route is not gated on the generate module"
        )


# --- malformed numeric fields must be a clean 400, not an uncaught 500 ----------
# `_gen_kwargs` did `int(body["n"])` and redesign did `float(body["strength"])`
# outside any try/except, so a non-numeric value crashed the request to a 500.


class _FakeReq:
    def __init__(self, body: dict, match_info: dict | None = None):
        self._body = body
        self.match_info = match_info or {}

    async def json(self):
        return self._body


def test_gen_kwargs_parses_valid_n():
    assert generation._gen_kwargs({"prompt": "x", "n": 3})["n"] == 3
    assert generation._gen_kwargs({"prompt": "x"})["n"] == 1  # default


def test_gen_kwargs_raises_on_non_integer_n():
    # The raise the handler now catches — under the old code it escaped to a 500.
    with pytest.raises((TypeError, ValueError)):
        generation._gen_kwargs({"prompt": "x", "n": "abc"})


async def test_media_generate_non_integer_n_is_400():
    resp = await generation.handle_media_generate(_FakeReq({"prompt": "hi", "n": "abc"}))
    assert resp.status == 400


async def test_media_redesign_non_numeric_strength_is_400():
    resp = await generation.handle_media_redesign(
        _FakeReq({"prompt": "hi", "strength": "abc"}, match_info={"id": "m1"})
    )
    assert resp.status == 400


# --- reference serving: a URL-supplied id must not build a path outside refs/ ----
# `handle_media_reference_raw` built `_reference_dir()/f"{id}.json"` from the raw URL
# segment. aiohttp's {id} excludes `/` but not `\`, so on Windows `..%5C..%5Cx` escaped
# the reference dir (path-traversal / arbitrary-file read). The id is now whitelisted.


def test_safe_reference_sidecar_accepts_uuid_and_rejects_traversal(tmp_path, monkeypatch):
    import uuid

    monkeypatch.setattr(generation, "_reference_dir", lambda: tmp_path)
    good = uuid.uuid4().hex
    assert generation._safe_reference_sidecar(good) == tmp_path / f"{good}.json"
    for bad in ("..\\..\\secret", "../../secret", "a/b", "a\\b", "a.b", "", "x" * 65, ".."):
        assert generation._safe_reference_sidecar(bad) is None, f"{bad!r} should be rejected"


async def test_reference_raw_rejects_windows_traversal(monkeypatch):
    # An unsafe id is rejected BEFORE any filesystem access (the regex runs first), so
    # this never touches the real data dir — and it must be a 404, not a 500 or a served
    # arbitrary file.
    resp = await generation.handle_media_reference_raw(
        _FakeReq({}, match_info={"id": "..\\..\\..\\Windows\\win.ini"})
    )
    assert resp.status == 404


async def test_reference_raw_rejects_dotted_id(monkeypatch):
    resp = await generation.handle_media_reference_raw(
        _FakeReq({}, match_info={"id": "a.b"})
    )
    assert resp.status == 404
