"""ElevenLabs wiring: Flows video/image, the audio tools, the library, and the CLI group.

Three contracts are pinned here, because each failure mode is silent in production:

* the Flows submit→poll body and the 402 ``paid_plan_required`` message a creator-tier
  key actually gets (surfaced from the BODY, never a bare status code);
* the multipart shape of the processing endpoints (field names are the contract —
  a renamed field is a 422 the user sees as "invalid request");
* the ``eleven`` command group stays mounted on ``generate_app`` — copying only
  ``registered_commands`` in core would drop the group while every flat command
  still passes.
"""

from __future__ import annotations

import base64

import pytest

from navig_generate.tools import audio_generation as ag
from navig_generate.tools import image_generation as ig
from navig_generate.tools import media_library as ml
from navig_generate.tools import video_generation as vg


# ── fakes ────────────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, payload=None, content=b"", status=200, headers=None, text=""):
        self._payload = payload
        self.content = content
        self.status_code = status
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Client:
    """Scripted httpx stand-in: posts answer once, gets answer in order."""

    is_closed = False

    def __init__(self, post=None, gets=None):
        self.post_resp = post if post is not None else _Resp({})
        self.get_resps = list(gets or [])
        self.calls: list[tuple[str, str, dict]] = []

    async def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self.post_resp

    async def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        if not self.get_resps:
            raise AssertionError(f"unexpected GET {url}")
        return self.get_resps.pop(0)

    async def aclose(self):
        self.is_closed = True

    def posted(self):
        return [c for c in self.calls if c[0] == "POST"][-1]

    def gotted(self):
        return [c for c in self.calls if c[0] == "GET"]


_PLAN_402 = _Resp(
    {"detail": {"type": "payment_required", "code": "paid_plan_required",
                "message": "This endpoint requires a Pro plan or above.",
                "status": "paid_plan_required"}},
    status=402,
)


@pytest.fixture
def eleven_key(monkeypatch):
    monkeypatch.setattr(vg, "resolve_media_key", lambda *a, **k: "sk-test")
    return "sk-test"


@pytest.fixture
def audio_gen(monkeypatch):
    monkeypatch.setattr(ag.AudioGenerator, "_api_key", lambda self: "k")
    g = ag.AudioGenerator(ag.AudioGenerationConfig(save_locally=False))
    yield g
    g._client = None


# ── Flows video ──────────────────────────────────────────────────────────────


async def test_flows_video_submit_then_poll(eleven_key):
    g = vg.VideoGenerator(vg.VideoGenerationConfig(poll_interval=0.01))
    client = _Client(
        post=_Resp({"id": "gen1", "status": "pending"}),
        gets=[_Resp({"id": "gen1", "status": "generating"}),
              _Resp({"id": "gen1", "status": "completed",
                     "content_url": "https://cdn.example/v.mp4",
                     "content_mime_type": "video/mp4"})],
    )
    g._client = client
    try:
        vid = await g.generate(
            "a lion running", provider=vg.VideoProvider.ELEVENLABS,
            seed=7, save=False,
            extra_input={"duration_secs": 5, "aspect_ratio": "16:9"},
        )
    finally:
        await g.close()

    posts = [c for c in client.calls if c[0] == "POST"]
    assert posts, "no POST recorded"
    _, post_url, post_kw = posts[0]
    assert post_url.endswith("/v1/flows/video")
    body = post_kw["json"]
    assert body["model_id"] == g.config.eleven_model
    assert body["prompt"] == "a lion running"
    assert body["seed"] == 7
    assert body["duration_secs"] == 5 and body["aspect_ratio"] == "16:9"
    assert post_kw["headers"]["xi-api-key"] == "sk-test"
    polls = [c[1] for c in client.calls if c[0] == "GET"]
    assert polls and polls[0].endswith("/v1/flows/video/gen1")
    assert vid.url == "https://cdn.example/v.mp4"
    assert vid.provider is vg.VideoProvider.ELEVENLABS


async def test_flows_video_402_quotes_the_plan_message(eleven_key):
    g = vg.VideoGenerator(vg.VideoGenerationConfig(poll_interval=0.01))
    g._client = _Client(post=_PLAN_402)
    try:
        with pytest.raises(vg.VideoGenerationError) as exc:
            await g.generate("x", provider=vg.VideoProvider.ELEVENLABS, save=False)
    finally:
        await g.close()
    assert "Pro plan or above" in str(exc.value)


async def test_flows_video_failure_reports_error_message(eleven_key):
    g = vg.VideoGenerator(vg.VideoGenerationConfig(poll_interval=0.01))
    g._client = _Client(
        post=_Resp({"id": "gen1", "status": "pending"}),
        gets=[_Resp({"id": "gen1", "status": "failed",
                     "failure_reason": "content_policy",
                     "error_message": "prompt rejected"})],
    )
    try:
        with pytest.raises(vg.VideoGenerationError) as exc:
            await g.generate("x", provider=vg.VideoProvider.ELEVENLABS, save=False)
    finally:
        await g.close()
    assert "prompt rejected" in str(exc.value)


async def test_flows_video_seed_image_inlined(eleven_key, tmp_path):
    png = tmp_path / "frame.png"
    png.write_bytes(b"\x89PNG-fake")
    g = vg.VideoGenerator(vg.VideoGenerationConfig(poll_interval=0.01))
    client = _Client(
        post=_Resp({"id": "gen1", "status": "pending"}),
        gets=[_Resp({"id": "gen1", "status": "completed",
                     "content_url": "https://cdn.example/v.mp4",
                     "content_mime_type": "video/mp4"})],
    )
    g._client = client
    try:
        await g.generate("x", provider=vg.VideoProvider.ELEVENLABS,
                         image_url=str(png), save=False)
    finally:
        await g.close()
    _, _, kw = [c for c in client.calls if c[0] == "POST"][0]
    ref = kw["json"]["start_frame"]
    assert ref["type"] == "inline_base64"
    assert ref["mime_type"] == "image/png"
    assert base64.b64decode(ref["content_base64"]) == b"\x89PNG-fake"


async def test_flows_video_url_seed_fetches_through_ssrf_guard(eleven_key, monkeypatch):
    """A caller-supplied seed URL is the module's only caller-controlled fetch — it
    must go through the shared SSRF guard, not the raw client (metadata/loopback)."""
    seen: dict[str, str] = {}

    async def _fake_safe_fetch(url, *a, **k):
        seen["url"] = url

        class _R:
            status_code = 200
            content = b"remote-bytes"
            headers = {"content-type": "image/png"}

        return _R()

    monkeypatch.setattr("navig_sdk.ssrf.safe_fetch", _fake_safe_fetch)
    g = vg.VideoGenerator(vg.VideoGenerationConfig(poll_interval=0.01))
    client = _Client(
        post=_Resp({"id": "gen1", "status": "pending"}),
        gets=[_Resp({"id": "gen1", "status": "completed",
                     "content_url": "https://cdn.example/v.mp4",
                     "content_mime_type": "video/mp4"})],
    )
    g._client = client
    try:
        await g.generate("x", provider=vg.VideoProvider.ELEVENLABS,
                         image_url="https://example.com/seed.png", save=False)
    finally:
        await g.close()
    assert seen.get("url") == "https://example.com/seed.png"
    # the fetched bytes are what reached the request body
    post = [c for c in client.calls if c[0] == "POST"][0]
    ref = post[2]["json"]["start_frame"]
    assert base64.b64decode(ref["content_base64"]) == b"remote-bytes"


# ── Flows image ──────────────────────────────────────────────────────────────


def test_eleven_aspect_and_resolution_maps_real_sizes():
    # Every mapping is a size NAVIG actually offers; a wrong ratio is a 422 or a
    # silently reframed image, so the pairs are pinned outright.
    cases = {
        "1024x1024": ("1:1", "1K"),
        "256x256": ("1:1", "1K"),
        # The LONG edge picks the bucket: anything over 1024 either way is 2K, so a
        # portrait 1024x1792 is never answered smaller than the size requested.
        "1024x1536": ("2:3", "2K"),
        "1536x1024": ("3:2", "2K"),
        "1024x1792": ("9:16", "2K"),
        "1792x1024": ("16:9", "2K"),
    }
    for size, expected in cases.items():
        got = ig.eleven_aspect_and_resolution(ig.ImageSize(size))
        assert got == expected, f"{size}: {got} != {expected}"


async def test_flows_image_submit_poll_download(monkeypatch):
    monkeypatch.setattr("navig_media_core.tools.media_providers.resolve_media_key",
                        lambda *a, **k: "sk-test")
    gen = ig.ImageGenerator(ig.ImageGenerationConfig())
    client = _Client(
        post=_Resp({"id": "img1", "status": "pending"}),
        gets=[
            _Resp({"id": "img1", "status": "generating"}),
            _Resp({"id": "img1", "status": "completed",
                   "content_url": "https://cdn.example/i.png",
                   "content_mime_type": "image/png"}),
            _Resp(content=b"png-bytes"),
        ],
    )
    gen._client = client
    try:
        imgs = await gen.generate(
            "a corgi", size=ig.ImageSize.PORTRAIT_GPT, n=1,
            provider=ig.ImageProvider.ELEVENLABS, save=False,
        )
    finally:
        await gen.close()

    assert len(imgs) == 1
    img = imgs[0]
    assert img.provider is ig.ImageProvider.ELEVENLABS
    assert base64.b64decode(img.b64_data) == b"png-bytes"
    posts = [c for c in client.calls if c[0] == "POST"]
    body = posts[0][2]["json"]
    assert body["aspect_ratio"] == "2:3" and body["resolution"] == "2K"
    assert body["model_id"] == gen.config.eleven_model


async def test_flows_image_402_quotes_the_plan_message(monkeypatch):
    monkeypatch.setattr("navig_media_core.tools.media_providers.resolve_media_key",
                        lambda *a, **k: "sk-test")
    gen = ig.ImageGenerator(ig.ImageGenerationConfig())
    gen._client = _Client(post=_PLAN_402)
    try:
        with pytest.raises(RuntimeError) as exc:
            await gen.generate("x", provider=ig.ImageProvider.ELEVENLABS, save=False)
    finally:
        await gen.close()
    assert "Pro plan or above" in str(exc.value)


# ── audio tools (multipart contracts) ────────────────────────────────────────


async def test_isolate_posts_audio_field(audio_gen):
    audio_gen._client = _Client(post=_Resp(content=b"clean-audio"))
    out = await audio_gen.isolate_voice(b"noisy")
    assert out == b"clean-audio"
    _, url, kw = audio_gen._client.posted()
    assert url.endswith("/v1/audio-isolation")
    assert "audio" in kw["files"]
    assert kw["files"]["audio"][1] == b"noisy"


async def test_change_voice_field_shapes(audio_gen):
    audio_gen._client = _Client(post=_Resp(content=b"reshaped"))
    out = await audio_gen.change_voice(
        b"src", "VOICE123", model_id="eleven_english_s2v2",
        voice_settings={"stability": 0.4}, remove_background_noise=True, seed=9,
    )
    assert out == b"reshaped"
    _, url, kw = audio_gen._client.posted()
    assert url.endswith("/v1/speech-to-speech/VOICE123")
    data = kw["data"]
    assert data["model_id"] == "eleven_english_s2v2"
    assert '"stability"' in data["voice_settings"]  # JSON-encoded for multipart
    assert data["remove_background_noise"] == "true"  # lowercase, as the API expects
    assert data["seed"] == "9"
    assert "audio" in kw["files"]


async def test_stems_validates_variation_and_sends_fields(audio_gen):
    with pytest.raises(ag.AudioGenerationError, match="stem_variation_id"):
        await audio_gen.separate_stems(b"x", stem_variation_id="three_stems_v1")

    audio_gen._client = _Client(post=_Resp(content=b"zipbytes"))
    out = await audio_gen.separate_stems(
        b"track", stem_variation_id="six_stems_v1", output_format="mp3_44100_128"
    )
    assert out == b"zipbytes"
    _, url, kw = audio_gen._client.posted()
    assert url.endswith("/v1/music/stem-separation?output_format=mp3_44100_128")
    assert kw["data"]["stem_variation_id"] == "six_stems_v1"
    assert kw["data"]["sign_with_c2pa"] == "false"
    assert "file" in kw["files"]


async def test_transcribe_defaults_to_scribe_and_returns_payload(audio_gen):
    audio_gen._client = _Client(post=_Resp({"text": "hello world", "words": [], "language_code": "eng"}))
    data = await audio_gen.transcribe(b"speech", keyterms=["NAVIG"])
    assert data["text"] == "hello world"
    _, url, kw = audio_gen._client.posted()
    assert url.endswith("/v1/speech-to-text")
    assert kw["data"]["model_id"] == "scribe_v2"
    assert '"NAVIG"' in kw["data"]["keyterms"]
    assert "file" in kw["files"]


async def test_upload_source_rejects_missing_and_empty(audio_gen, tmp_path):
    with pytest.raises(ag.AudioGenerationError, match="not found"):
        await audio_gen.isolate_voice(tmp_path / "nope.mp3")
    with pytest.raises(ag.AudioGenerationError, match="empty"):
        await audio_gen.isolate_voice(b"")


# ── library ──────────────────────────────────────────────────────────────────


@pytest.fixture
def library(monkeypatch):
    monkeypatch.setattr(ml.MediaLibrary, "_api_key", lambda self: "k")
    lib = ml.MediaLibrary()
    yield lib
    lib._client = None


async def test_history_passes_paging_and_search(library):
    library._client = _Client(post=_Resp({}), gets=[_Resp({"history": [], "has_more": False})])
    await library.history(page_size=5, search="lion", voice_id="v1")
    _, url, kw = library._client.gotted()[0]
    assert url.endswith("/v1/history")
    assert kw["params"] == {"page_size": 5, "search": "lion", "voice_id": "v1"}


async def test_history_download_posts_ids(library):
    library._client = _Client(post=_Resp(content=b"PK-zip"))
    out = await library.history_download(["a", "b"])
    assert out == b"PK-zip"
    _, url, kw = library._client.posted()
    assert url.endswith("/v1/history/download")
    assert kw["json"] == {"history_item_ids": ["a", "b"]}

    with pytest.raises(ml.AudioGenerationError, match="at least one"):
        await library.history_download([])


async def test_shared_voices_search_params(library):
    library._client = _Client(gets=[_Resp({"voices": [{"name": "Rachel", "voice_id": "v"}]})])
    data = await library.shared_voices(search="calm narrator", language="en")
    assert data["voices"][0]["name"] == "Rachel"
    _, _, kw = library._client.gotted()[0]
    assert kw["params"]["search"] == "calm narrator"
    assert kw["params"]["language"] == "en"


def test_write_output_file_refuses_empty(tmp_path):
    with pytest.raises(ml.AudioGenerationError, match="no bytes"):
        ml.write_output_file(b"", tmp_path / "out.mp3")
    p = ml.write_output_file(b"audio", tmp_path / "sub" / "out.mp3")
    assert p.read_bytes() == b"audio"


# ── catalog + CLI surface ────────────────────────────────────────────────────


def test_catalog_lists_elevenlabs_for_image_video_and_audio():
    from navig_media_core.tools.media_providers import MEDIA_CATALOG

    for modality in ("image", "video", "audio"):
        ids = [e["id"] for e in MEDIA_CATALOG[modality]]
        assert "elevenlabs" in ids, f"elevenlabs missing from {modality}"
    # the audio `models` list feeds a "--kind" picker elsewhere: only kinds that
    # AudioKind accepts may appear, or a picker choice becomes a ValueError.
    audio = next(e for e in MEDIA_CATALOG["audio"] if e["id"] == "elevenlabs")
    assert [m["id"] for m in audio["models"]] == ["music", "sfx", "tts"]


def test_eleven_group_mounts_on_generate_app():
    from navig_generate.commands.generate import eleven_app, generate_app

    assert "eleven" in [g.name for g in generate_app.registered_groups]
    verbs = {c.name for c in eleven_app.registered_commands}
    assert {"subscription", "history", "fetch", "voices",
            "isolate", "change-voice", "stems", "transcribe"} <= verbs


# ── CLI bodies actually execute (a mounted command can still NameError) ─────


def _run_cli(args: list[str]):
    from typer.testing import CliRunner

    from navig_generate.commands.generate import generate_app

    return CliRunner().invoke(generate_app, args)


def test_eleven_help_lists_every_verb():
    res = _run_cli(["eleven", "--help"])
    assert res.exit_code == 0, res.output
    for verb in ("subscription", "history", "fetch", "voices",
                 "isolate", "change-voice", "stems", "transcribe"):
        assert verb in res.output


def test_cli_subscription_renders_the_plan(monkeypatch):
    from navig_generate.tools.audio_generation import AudioGenerator

    async def _sub(self):
        return {"tier": "creator", "status": "active", "character_count": 10,
                "character_limit": 100, "voice_slots_used": 1, "voice_limit": 30,
                "can_use_instant_voice_cloning": True,
                "can_use_professional_voice_cloning": True}

    monkeypatch.setattr(AudioGenerator, "subscription", _sub)
    res = _run_cli(["eleven", "subscription"])
    assert res.exit_code == 0, res.output
    assert "creator" in res.output and "10 / 100" in res.output


def test_cli_voices_mine_path_runs(monkeypatch):
    """--mine used to call a function that did not exist; execute the body, not just mount it."""
    from navig_generate.tools.audio_generation import AudioGenerator

    async def _voices(self):
        return [{"voice_id": "v1", "name": "My Voice", "language": "en"}]

    monkeypatch.setattr(AudioGenerator, "list_voices", _voices)
    res = _run_cli(["eleven", "voices", "--mine"])
    assert res.exit_code == 0, res.output
    assert "My Voice" in res.output and "v1" in res.output


async def test_flows_image_batch_offsets_a_supplied_seed(monkeypatch):
    """n=3 with seed=42 must submit 42/43/44 — identical bodies would bill a
    deterministic image three times instead of rendering a batch."""
    monkeypatch.setattr("navig_media_core.tools.media_providers.resolve_media_key",
                        lambda *a, **k: "sk-test")
    gen = ig.ImageGenerator(ig.ImageGenerationConfig())
    # per iteration: submit(pending) -> poll(completed) -> download
    gets = []
    for _ in range(3):
        gets.append(_Resp({"id": "g", "status": "completed",
                           "content_url": "https://cdn.example/i.png",
                           "content_mime_type": "image/png"}))
        gets.append(_Resp(content=b"png"))
    client = _Client(post=_Resp({"id": "g", "status": "pending"}), gets=gets)
    gen._client = client
    try:
        imgs = await gen.generate("x", n=3, seed=42,
                                  provider=ig.ImageProvider.ELEVENLABS, save=False)
    finally:
        await gen.close()
    assert len(imgs) == 3
    bodies = [c[2]["json"] for c in client.calls if c[0] == "POST"]
    assert [b.get("seed") for b in bodies] == [42, 43, 44]


def test_cli_voices_mine_honours_search_language_and_limit(monkeypatch):
    """--mine used to list every voice unfiltered: the API returns the whole shelf."""
    from navig_generate.tools.audio_generation import AudioGenerator

    async def _voices(self):
        return [
            {"voice_id": "v1", "name": "Alice Calm", "language": "en"},
            {"voice_id": "v2", "name": "Bob Ruiz", "language": "es"},
            {"voice_id": "v3", "name": "Alicia Norte", "language": "en"},
        ]

    monkeypatch.setattr(AudioGenerator, "list_voices", _voices)

    res = _run_cli(["eleven", "voices", "--mine", "-s", "ali"])
    assert res.exit_code == 0, res.output
    assert "Alice Calm" in res.output and "Alicia Norte" in res.output
    assert "Bob Ruiz" not in res.output

    res = _run_cli(["eleven", "voices", "--mine", "--language", "es"])
    assert res.exit_code == 0, res.output
    assert "Bob Ruiz" in res.output and "Alice Calm" not in res.output

    res = _run_cli(["eleven", "voices", "--mine", "-n", "1"])
    assert res.exit_code == 0, res.output
    assert "Alice Calm" in res.output and "Alicia Norte" not in res.output
