"""Audio Enhance deck routes: gating, upload → analyze → enhance job → variant, cancel, export.

The media store and refs library are pinned to temp paths (the store is a process
singleton and the refs root defaults to the ACTIVE SPACE), so nothing here touches
the operator's library.
"""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import wave
from pathlib import Path

import numpy as np
import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

from navig_audio.deck_routes import enhance as routes  # noqa: E402

SR = 44100


def _wav_bytes(x: np.ndarray) -> bytes:
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _clipped() -> np.ndarray:
    t = np.arange(int(2.5 * SR)) / SR
    x = np.zeros_like(t)
    for t0 in (0.3, 1.0, 1.7):
        m = t >= t0
        x[m] += 1.8 * np.sin(2 * np.pi * 440 * (t[m] - t0)) * np.exp(-(t[m] - t0) * 5)
    return np.clip(x, -1, 1)


@pytest.fixture
def media(tmp_path, monkeypatch):
    from navig_media_core import generated_media
    from navig_media_core.media import refs_library

    store = generated_media.GeneratedMediaStore(tmp_path / "generated_media.db")
    monkeypatch.setattr(generated_media, "_store", store)
    space = tmp_path / "space"
    monkeypatch.setattr(refs_library, "resolve_refs_root",
                        lambda space_dir=None: Path(space_dir or space) / ".navig" / "refs")
    monkeypatch.setenv("NAVIG_DATA_DIR", str(tmp_path / "data"))
    routes._JOBS.clear()
    yield {"store": store, "space": space, "tmp": tmp_path}
    store.close()


class _Field:
    def __init__(self, name: str, data: bytes):
        self.filename = name
        self.file = io.BytesIO(data)


class _Req:
    def __init__(self, body=None, match_info=None, query=None, post=None, length=0):
        self._body = body
        self.match_info = match_info or {}
        self.query = query or {}
        self._post = post
        self.content_length = length

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body

    async def post(self):
        return self._post or {}


def _b(resp):
    return resp.status, json.loads(resp.body.decode())


EXPECTED = {
    ("GET", "/api/deck/audio/enhance/presets"), ("POST", "/api/deck/audio/enhance/upload"),
    ("GET", "/api/deck/audio/enhance/jobs/{job}"), ("POST", "/api/deck/audio/enhance/jobs/{job}/cancel"),
    ("POST", "/api/deck/media/{id}/analyze"), ("GET", "/api/deck/media/{id}/waveform"),
    ("POST", "/api/deck/media/{id}/enhance"), ("POST", "/api/deck/media/{id}/export"),
}


def test_routes_register_and_are_gated_on_audio():
    app = web.Application()
    routes.register(app)
    got = {}
    for r in app.router.routes():
        info = r.resource.get_info()
        got[(r.method, info.get("path") or info.get("formatter") or "")] = r.handler
    assert EXPECTED <= set(got)
    for key in EXPECTED:
        assert getattr(got[key], "__navig_required_module__", None) == "audio", key


async def _upload(name: str, data: bytes):
    return _b(await routes.handle_upload(_Req(post={"file": _Field(name, data)}, length=len(data))))


async def _wait(job_id: str, timeout: float = 180.0) -> dict:  # a loaded box runs jobs slowly
    for _ in range(int(timeout * 10)):
        st, body = _b(await routes.handle_job(_Req(match_info={"job": job_id})))
        if body["data"]["state"] != "running":
            return body["data"]
        await asyncio.sleep(0.1)
    raise AssertionError("job did not finish")


async def test_upload_analyze_enhance_creates_a_verified_variant(media):
    st, body = await _upload("take one.wav", _wav_bytes(_clipped()))
    assert st == 201, body
    src = body["data"]
    assert src["modality"] == "audio" and Path(src["path"]).is_file()
    assert str(media["tmp"]) in src["path"]  # isolated: the library is the temp space
    before = Path(src["path"]).read_bytes()

    st, body = _b(await routes.handle_analyze(_Req({}, {"id": src["id"]})))
    assert st == 200 and any(f["code"] == "clipping" for f in body["data"]["findings"])

    st, body = _b(await routes.handle_waveform(_Req(match_info={"id": src["id"]}, query={"points": "200"})))
    assert st == 200 and body["data"]["points"] == 200 and len(body["data"]["max"]) == 200

    st, body = _b(await routes.handle_enhance(_Req({"preset_id": "audio-enhance/preserve-original"},
                                                   {"id": src["id"]})))
    assert st == 202, body
    job = await _wait(body["data"]["job_id"])
    assert job["state"] == "done", job
    res = job["result"]
    assert res["verdict"] in ("pass", "warn")
    variant = res["variant"]
    assert variant["group_id"] == src["group_id"] and variant["id"] != src["id"]
    assert variant["context"]["derived_from"] == src["id"] and variant["context"]["op"] == "enhance"
    assert Path(src["path"]).read_bytes() == before  # the source variant is untouched

    st, body = _b(await routes.handle_export(_Req({"format": "flac", "dir": str(media["tmp"])},
                                                  {"id": variant["id"]})))
    assert st == 200 and body["data"]["files"][0].endswith(".enhanced.flac")


async def test_upload_refuses_non_audio(media):
    st, body = await _upload("notes.txt", b"hello")
    assert st == 415
    st, body = await _upload("fake.wav", b"definitely not riff data" * 50)
    assert st == 415


async def test_upload_size_limit(media, monkeypatch):
    monkeypatch.setattr(routes, "MAX_UPLOAD_BYTES", 10)
    st, body = _b(await routes.handle_upload(_Req(post={"file": _Field("a.wav", b"x" * 50)}, length=50)))
    assert st == 413


async def test_cloud_steps_need_the_request_to_allow_them(media, monkeypatch):
    st, body = await _upload("v.wav", _wav_bytes(_clipped()))
    seen = {}

    def fake_enhance(path, **kw):
        seen.update(kw)
        raise RuntimeError("stop here")

    import navig_audio.enhance as pkg

    monkeypatch.setattr(pkg, "enhance", fake_enhance)
    st, resp = _b(await routes.handle_enhance(_Req({"mode": "preserve", "params": {"engine": "elevenlabs",
                                                                                     "allow_cloud": True}},
                                                   {"id": body["data"]["id"]})))
    job = await _wait(resp["data"]["job_id"])
    assert job["state"] == "failed" and seen["params"]["allow_cloud"] is False


async def test_bad_inputs_are_400s_not_500s(media):
    st, _ = _b(await routes.handle_enhance(_Req({}, {"id": "nope"})))
    assert st == 404
    st, body = await _upload("c.wav", _wav_bytes(_clipped()))
    mid = body["data"]["id"]
    st, resp = _b(await routes.handle_enhance(_Req({"preset_id": "audio-enhance/nope"}, {"id": mid})))
    assert st == 400
    st, resp = _b(await routes.handle_export(_Req({"format": "ogg"}, {"id": mid})))
    assert st == 400
    st, resp = _b(await routes.handle_export(_Req({"format": "wav", "dir": "relative/dir"}, {"id": mid})))
    assert st == 400
    st, resp = _b(await routes.handle_job(_Req(match_info={"job": "..\\..\\x"})))
    assert st == 404


async def test_cancel_marks_the_job_cancelled(media, monkeypatch):
    st, body = await _upload("c.wav", _wav_bytes(_clipped()))
    import navig_audio.enhance as pkg
    from navig_audio.enhance import Cancelled

    def slow(path, cancel=None, **kw):
        for _ in range(200):
            if cancel.is_set():
                raise Cancelled("cancelled")
            import time

            time.sleep(0.02)
        raise AssertionError("never cancelled")

    monkeypatch.setattr(pkg, "enhance", slow)
    st, resp = _b(await routes.handle_enhance(_Req({"mode": "preserve"}, {"id": body["data"]["id"]})))
    jid = resp["data"]["job_id"]
    await asyncio.sleep(0.1)
    await routes.handle_cancel(_Req(match_info={"job": jid}))
    job = await _wait(jid)
    assert job["state"] == "cancelled"


async def test_presets_route_lists_the_shipped_modes(media):
    st, body = _b(await routes.handle_presets(_Req()))
    ids = {p["id"] for p in body["data"]["presets"]}
    assert {"audio-enhance/preserve-original", "audio-enhance/clean-sfx", "audio-enhance/studio-polish",
            "audio-enhance/voice-restoration", "audio-enhance/music-mastering",
            "audio-enhance/cinematic-creative"} <= ids
    assert body["data"]["modes"] == ["preserve", "studio", "cinematic", "custom"]


# ── AI regeneration routes (2026-10-16) ──────────────────────────────


async def test_regenerate_route_needs_consent_and_saves_new_variants(media, monkeypatch):
    from tests.test_regenerate import FakeProvider

    import navig_audio.regenerate.core as core

    fake = FakeProvider()
    monkeypatch.setattr(core, "ElevenLabsProvider", lambda: fake)
    st, body = await _upload("money.wav", _wav_bytes(_clipped()))
    mid = body["data"]["id"]

    st, resp = _b(await routes.handle_regenerate(_Req({"op": "variations"}, {"id": mid})))
    assert st == 400 and "allow_cloud" in resp["error"] and fake.calls == []

    st, resp = _b(await routes.handle_regenerate(_Req({"op": "variations", "dry_run": True}, {"id": mid})))
    assert st == 200 and resp["data"]["plan"]["uploads_source"] is False and fake.calls == []

    st, resp = _b(await routes.handle_regenerate(_Req({"op": "variations", "allow_cloud": True, "count": 2,
                                                         "direction": "heavier"}, {"id": mid})))
    assert st == 202
    job = await _wait(resp["data"]["job_id"])
    assert job["state"] == "done", job
    variants = job["result"]["variants"]
    assert len(variants) == 2
    for v in variants:
        assert v["group_id"] == body["data"]["group_id"] and v["context"]["generated"] is True
        assert v["context"]["op"] == "regenerate:variations" and v["context"]["derived_from"] == mid


async def test_regenerate_route_rejects_bad_ops(media):
    st, body = await _upload("a.wav", _wav_bytes(_clipped()))
    st, resp = _b(await routes.handle_regenerate(_Req({"op": "remix", "allow_cloud": True}, {"id": body["data"]["id"]})))
    assert st == 400
    st, resp = _b(await routes.handle_regenerate(_Req({"op": "voice"}, {"id": "missing"})))
    assert st == 404


def test_regenerate_routes_are_registered_and_gated():
    app = web.Application()
    routes.register(app)
    got = {(r.method, (r.resource.get_info().get("path") or r.resource.get_info().get("formatter"))): r.handler
           for r in app.router.routes()}
    for key in (("POST", "/api/deck/media/{id}/regenerate"), ("GET", "/api/deck/audio/voices")):
        assert getattr(got[key], "__navig_required_module__", None) == "audio", key
