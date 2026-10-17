"""
NAVIG Video Generation Tool

Text-to-video (and image-to-video) across providers:
- Google Veo (via the Gemini API)   — free tier through Google AI Studio
- Replicate                         — hosted catalog (Kling, Luma, Wan, …)
- Runway (Gen-4)                    — cinematic, paid
- Luma Dream Machine (Ray)          — dreamy motion, paid
- ElevenLabs Flows (Veo 3.1, ByteDance Seedance) — one ElevenLabs subscription;
  submit → poll → signed URL. Pro plan or above (a creator key gets 402).

All four are asynchronous jobs (submit → poll → download). Each driver submits
the job, polls until the video URL is ready (or times out), and returns a
`GeneratedVideo`. Keys resolve through the shared media resolver (env → vault),
so a key stored in the Deck vault works without exporting an env var.

Nothing here modifies or deletes any source; it only produces new files.
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from navig_sdk.host import media_dir

if TYPE_CHECKING:
    import httpx

try:
    import httpx

    HTTPX_AVAILABLE = True
except ImportError:
    httpx = None  # type: ignore[assignment]
    HTTPX_AVAILABLE = False

from navig_generate._compat import get_logger
from navig_media_core.tools.media_providers import resolve_media_key

logger = get_logger("tools.video_generation")

# How many times to wait out a 429 before giving up. Replicate's throttle window is
# seconds, not minutes, so a handful of retries covers it without hiding a real problem.
_THROTTLE_RETRIES = 4


class VideoGenerationError(RuntimeError):
    """The provider refused the job — credit, plan, key, or a malformed request."""


def _check(resp: Any, what: str) -> None:
    """Raise with the provider's OWN message rather than a bare status code.

    The failures that actually happen are 401 (bad key) and 402 (no credit), and those
    are entirely different actions for the user. `raise_for_status()` reports neither —
    it says "Client error '402 Payment Required'", while the body says exactly where to
    go and to wait a few minutes after paying.
    """
    if resp.status_code < 400:
        return
    detail: Any = None
    try:
        payload = resp.json()
        if isinstance(payload, dict):
            detail = payload.get("detail") or payload.get("title") or payload
        else:
            detail = payload
    except Exception:  # noqa: BLE001 — a non-JSON error body is still worth reporting
        detail = (resp.text or "").strip()[:300] or None
    raise VideoGenerationError(
        f"{what} failed ({resp.status_code}): {detail or 'no detail returned'}"
    )


class VideoProvider(Enum):
    """Supported video generation providers."""

    GEMINI_VEO = "gemini_veo"
    REPLICATE = "replicate"
    RUNWAY = "runway"
    LUMA = "luma"
    # ElevenLabs Flows — rides the ElevenLabs subscription instead of a second
    # video bill. NOTE: the flows endpoints answer 402 `paid_plan_required` on a
    # creator-tier key (measured 2026-10-14); a Pro plan or above is required.
    ELEVENLABS = "elevenlabs"


@dataclass
class VideoGenerationConfig:
    """Configuration for video generation."""

    provider: VideoProvider = VideoProvider.GEMINI_VEO

    # Model overrides per provider (release ids drift; keep swappable).
    veo_model: str = "veo-3.0-generate-preview"
    replicate_model: str = "kwaivgi/kling-v2.1"
    # Which input a Replicate model calls its seed image. Kling says `start_image`, Wan's
    # image-to-video says `image`, and Replicate rejects an input key a model does not
    # declare — so seeding the wrong name is a 422, not a silently ignored extra.
    replicate_image_key: str = "start_image"
    runway_model: str = "gen4_turbo"
    luma_model: str = "ray-2"
    # ElevenLabs Flows video models (the catalog lists them all): veo-3.1-fast /
    # veo-3.1 / bytedance-seedance-v2{,-fast,-mini,-2.5} / creatify-aurora.
    eleven_model: str = "veo-3.1-fast-generate-001"

    # Job polling
    poll_interval: float = 5.0
    poll_timeout: float = 300.0  # 5 min — video jobs are slow

    # Output
    output_dir: str = field(
        default_factory=lambda: str(media_dir("videos"))
    )
    save_locally: bool = True

    @classmethod
    def from_env(cls) -> VideoGenerationConfig:
        return cls(provider=VideoProvider(os.environ.get("VIDEO_PROVIDER", "gemini_veo")))


@dataclass
class GeneratedVideo:
    """A generated video result."""

    prompt: str
    provider: VideoProvider
    url: str | None = None
    local_path: str | None = None
    model: str | None = None
    duration_s: float | None = None
    generation_time: float = 0.0
    created_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "provider": self.provider.value,
            "url": self.url,
            "local_path": self.local_path,
            "model": self.model,
            "duration_s": self.duration_s,
            "generation_time": self.generation_time,
            "created_at": self.created_at.isoformat(),
        }

    async def save_to_file(self, filepath: str, client: httpx.AsyncClient) -> str:
        """Download the video URL to a local file."""
        if not self.url:
            raise ValueError("No video URL available to save")
        path = Path(filepath).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        resp = await client.get(self.url)
        resp.raise_for_status()
        path.write_bytes(resp.content)
        self.local_path = str(path)
        return str(path)


class VideoGenerator:
    """Multi-provider video generation client."""

    def __init__(self, config: VideoGenerationConfig | None = None):
        if not HTTPX_AVAILABLE:
            raise ImportError("httpx is required for video generation. Install: pip install httpx")
        self.config = config or VideoGenerationConfig.from_env()
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=120.0)
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def upload_image(self, path: Path | str) -> str:
        """Put a local image where Replicate can fetch it, and return that URL.

        Image-to-video needs the seed frame to be reachable by the provider, and a local
        path is not. A data URI is the obvious shortcut and the wrong one here: the frames
        this is given are megabyte-scale PNGs, and inlining one bloats the request body far
        past what the predictions endpoint will take. Replicate's own Files API exists for
        exactly this, so use it and hand back the served URL.
        """
        token = resolve_media_key("replicate", "REPLICATE_API_TOKEN")
        if not token:
            raise VideoGenerationError("Replicate API token not configured")
        src = Path(path)
        if not src.exists():
            raise VideoGenerationError(f"seed image not found: {src}")
        suffix = src.suffix.lower().lstrip(".") or "png"
        mime = {"jpg": "jpeg", "jpe": "jpeg"}.get(suffix, suffix)
        client = await self._get_client()
        resp = await client.post(
            "https://api.replicate.com/v1/files",
            headers={"Authorization": f"Bearer {token}"},
            files={"content": (src.name, src.read_bytes(), f"image/{mime}")},
        )
        _check(resp, f"uploading {src.name}")
        url = ((resp.json() or {}).get("urls") or {}).get("get")
        if not url:
            raise VideoGenerationError(
                f"Replicate accepted {src.name} but returned no URL to fetch it from"
            )
        return str(url)

    async def generate(
        self,
        prompt: str,
        provider: VideoProvider | None = None,
        image_url: str | None = None,
        save: bool = True,
        seed: int | None = None,
        extra_input: dict[str, Any] | None = None,
    ) -> GeneratedVideo:
        """Generate a video from a text prompt (optionally seeded with an image).

        `seed` is honored by Replicate models that accept it; Veo/Runway/Luma
        have no public seed param and ignore it (best-effort reproducibility).
        """
        provider = provider or self.config.provider

        if provider == VideoProvider.GEMINI_VEO:
            video = await self._generate_veo(prompt, image_url)
        elif provider == VideoProvider.REPLICATE:
            video = await self._generate_replicate(prompt, image_url, seed, extra_input)
        elif provider == VideoProvider.RUNWAY:
            video = await self._generate_runway(prompt, image_url)
        elif provider == VideoProvider.LUMA:
            video = await self._generate_luma(prompt, image_url)
        elif provider == VideoProvider.ELEVENLABS:
            video = await self._generate_elevenlabs(prompt, image_url, seed, extra_input)
        else:
            raise ValueError(f"Unsupported provider: {provider}")

        if save and self.config.save_locally and video.url:
            out = Path(self.config.output_dir).expanduser()
            out.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            try:
                await video.save_to_file(str(out / f"video_{ts}.mp4"), await self._get_client())
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to save video: %s", e)
        return video

    # ── Google Veo (Gemini API, long-running operation) ──────────────────────
    async def _generate_veo(self, prompt: str, image_url: str | None) -> GeneratedVideo:
        api_key = resolve_media_key("google", "GEMINI_API_KEY", "GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("Google (Gemini) API key not configured")
        client = await self._get_client()
        model = self.config.veo_model
        headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
        start = datetime.now()

        # 1) Submit the long-running predict operation.
        resp = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:predictLongRunning",
            headers=headers,
            json={"instances": [{"prompt": prompt}]},
        )
        resp.raise_for_status()
        op_name = resp.json().get("name")
        if not op_name:
            raise RuntimeError("Veo: no operation name returned")

        # 2) Poll the operation until done.
        deadline = start.timestamp() + self.config.poll_timeout
        while True:
            await asyncio.sleep(self.config.poll_interval)
            op = await client.get(
                f"https://generativelanguage.googleapis.com/v1beta/{op_name}",
                headers=headers,
            )
            op.raise_for_status()
            body = op.json()
            if body.get("done"):
                resp_body = body.get("response", {})
                samples = (
                    resp_body.get("generateVideoResponse", {}).get("generatedSamples")
                    or resp_body.get("generatedSamples")
                    or []
                )
                url = None
                if samples:
                    url = (samples[0].get("video") or {}).get("uri") or samples[0].get("uri")
                # Veo download URIs need the key appended.
                if url and "key=" not in url:
                    url = f"{url}{'&' if '?' in url else '?'}key={api_key}"
                return GeneratedVideo(
                    prompt=prompt,
                    provider=VideoProvider.GEMINI_VEO,
                    url=url,
                    model=model,
                    generation_time=(datetime.now() - start).total_seconds(),
                )
            if datetime.now().timestamp() > deadline:
                raise TimeoutError("Veo generation timed out")

    # ── Replicate (predictions API) ──────────────────────────────────────────
    async def _generate_replicate(
        self, prompt: str, image_url: str | None, seed: int | None = None,
        extra_input: dict[str, Any] | None = None,
    ) -> GeneratedVideo:
        token = resolve_media_key("replicate", "REPLICATE_API_TOKEN")
        if not token:
            raise ValueError("Replicate API token not configured")
        client = await self._get_client()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        start = datetime.now()
        model_input: dict[str, Any] = {"prompt": prompt}
        if seed is not None:
            model_input["seed"] = seed
        if image_url:
            model_input[self.config.replicate_image_key] = image_url
        # Model-specific inputs the caller knows about and this client cannot: aspect
        # ratio, resolution, frame count. Without a way to ask for 9:16, vertical output
        # can only be reached by generating widescreen and cropping ~70% of it away,
        # which throws out the framing the model was asked for.
        if extra_input:
            model_input.update(extra_input)

        # Uses the model-scoped predictions endpoint (official model slug).
        url = f"https://api.replicate.com/v1/models/{self.config.replicate_model}/predictions"
        # Replicate throttles hard on a low balance — "6 requests per minute with a burst
        # of 1 while you have less than $5.0 in credit". A reel submits one job per shot,
        # so the SECOND shot fails immediately unless the throttle is waited out. The
        # response says exactly how long to wait, so honour it rather than failing a job
        # that would succeed four seconds later.
        for attempt in range(_THROTTLE_RETRIES):
            resp = await client.post(url, headers=headers, json={"input": model_input})
            if resp.status_code != 429 or attempt == _THROTTLE_RETRIES - 1:
                break
            try:
                wait = float(resp.json().get("retry_after") or 5)
            except Exception:  # noqa: BLE001 — a malformed throttle body still means wait
                wait = 5.0
            logger.info("Replicate throttled; retrying in %.0fs", wait)
            await asyncio.sleep(min(wait, 60) + 1)
        _check(resp, f"Replicate ({self.config.replicate_model})")
        pred = resp.json()
        get_url = pred.get("urls", {}).get("get")

        deadline = start.timestamp() + self.config.poll_timeout
        while pred.get("status") not in ("succeeded", "failed", "canceled"):
            if datetime.now().timestamp() > deadline:
                raise TimeoutError("Replicate generation timed out")
            await asyncio.sleep(self.config.poll_interval)
            poll = await client.get(get_url, headers=headers)
            _check(poll, "Replicate poll")
            pred = poll.json()

        if pred.get("status") != "succeeded":
            raise RuntimeError(f"Replicate job {pred.get('status')}: {pred.get('error')}")
        output = pred.get("output")
        url = output[0] if isinstance(output, list) and output else output
        return GeneratedVideo(
            prompt=prompt,
            provider=VideoProvider.REPLICATE,
            url=url,
            model=self.config.replicate_model,
            generation_time=(datetime.now() - start).total_seconds(),
        )

    # ── Runway (Gen-4, tasks API) ────────────────────────────────────────────
    async def _generate_runway(self, prompt: str, image_url: str | None) -> GeneratedVideo:
        key = resolve_media_key("runway", "RUNWAYML_API_SECRET", "RUNWAY_API_KEY")
        if not key:
            raise ValueError("Runway API key not configured")
        client = await self._get_client()
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-Runway-Version": "2024-11-06",
        }
        start = datetime.now()
        # Runway's text→video path requires a seed image for image_to_video;
        # a promptText-only job uses text_to_video where available.
        endpoint = "image_to_video" if image_url else "text_to_video"
        payload: dict[str, Any] = {"model": self.config.runway_model, "promptText": prompt}
        if image_url:
            payload["promptImage"] = image_url
        resp = await client.post(
            f"https://api.dev.runwayml.com/v1/{endpoint}", headers=headers, json=payload
        )
        resp.raise_for_status()
        task_id = resp.json().get("id")

        deadline = start.timestamp() + self.config.poll_timeout
        while True:
            if datetime.now().timestamp() > deadline:
                raise TimeoutError("Runway generation timed out")
            await asyncio.sleep(self.config.poll_interval)
            poll = await client.get(
                f"https://api.dev.runwayml.com/v1/tasks/{task_id}", headers=headers
            )
            poll.raise_for_status()
            task = poll.json()
            status = task.get("status")
            if status == "SUCCEEDED":
                output = task.get("output") or []
                url = output[0] if output else None
                return GeneratedVideo(
                    prompt=prompt,
                    provider=VideoProvider.RUNWAY,
                    url=url,
                    model=self.config.runway_model,
                    generation_time=(datetime.now() - start).total_seconds(),
                )
            if status in ("FAILED", "CANCELLED"):
                raise RuntimeError(f"Runway task {status}: {task.get('failure')}")

    # ── Luma Dream Machine (generations API) ─────────────────────────────────
    async def _generate_luma(self, prompt: str, image_url: str | None) -> GeneratedVideo:
        key = resolve_media_key("luma", "LUMAAI_API_KEY", "LUMA_API_KEY")
        if not key:
            raise ValueError("Luma API key not configured")
        client = await self._get_client()
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        start = datetime.now()
        payload: dict[str, Any] = {"prompt": prompt, "model": self.config.luma_model}
        if image_url:
            payload["keyframes"] = {"frame0": {"type": "image", "url": image_url}}
        resp = await client.post(
            "https://api.lumalabs.ai/dream-machine/v1/generations", headers=headers, json=payload
        )
        resp.raise_for_status()
        gen_id = resp.json().get("id")

        deadline = start.timestamp() + self.config.poll_timeout
        while True:
            if datetime.now().timestamp() > deadline:
                raise TimeoutError("Luma generation timed out")
            await asyncio.sleep(self.config.poll_interval)
            poll = await client.get(
                f"https://api.lumalabs.ai/dream-machine/v1/generations/{gen_id}", headers=headers
            )
            poll.raise_for_status()
            gen = poll.json()
            state = gen.get("state")
            if state == "completed":
                url = (gen.get("assets") or {}).get("video")
                return GeneratedVideo(
                    prompt=prompt,
                    provider=VideoProvider.LUMA,
                    url=url,
                    model=self.config.luma_model,
                    generation_time=(datetime.now() - start).total_seconds(),
                )
            if state == "failed":
                raise RuntimeError(f"Luma generation failed: {gen.get('failure_reason')}")

    # ── ElevenLabs Flows (submit → poll → signed content_url) ────────────────
    async def _generate_elevenlabs(
        self,
        prompt: str,
        image_url: str | None,
        seed: int | None = None,
        extra_input: dict[str, Any] | None = None,
    ) -> GeneratedVideo:
        """Generate via ElevenLabs Flows — one subscription across audio and video.

        Two shapes of honesty here that the other providers do not have:

        * **Plan gating.** Every flows endpoint answers ``402 paid_plan_required``
          ("This endpoint requires a Pro plan or above.") on a creator-tier key,
          so ``_check`` quoting the response BODY — not the status code — is what
          tells the user to upgrade instead of printing "Client error 402".
        * **A signed, expiring URL.** ``content_url`` is valid for about an hour;
          ``generate()`` downloads it immediately on save, which is why the caller
          must not sit on the returned URL.
        """
        key = resolve_media_key("elevenlabs", "ELEVENLABS_API_KEY", "ELEVEN_API_KEY")
        if not key:
            raise ValueError("ElevenLabs API key not configured")
        client = await self._get_client()
        headers = {"xi-api-key": key, "Content-Type": "application/json"}
        model = self.config.eleven_model
        body: dict[str, Any] = {"model_id": model, "prompt": prompt}
        if seed is not None:
            body["seed"] = int(seed)
        if image_url:
            body["start_frame"] = await self._eleven_image_reference(image_url)
        # Model options the caller knows about (duration / framing / audio) — the
        # same pass-through role extra_input plays for Replicate.
        if extra_input:
            for k in (
                "negative_prompt", "enhance_prompt", "duration_secs",
                "aspect_ratio", "resolution", "generate_audio",
            ):
                if k in extra_input:
                    body[k] = extra_input[k]

        start = datetime.now()
        resp = await client.post(
            "https://api.elevenlabs.io/v1/flows/video", headers=headers, json=body
        )
        _check(resp, f"ElevenLabs Flows ({model})")
        gen_id = (resp.json() or {}).get("id")
        if not gen_id:
            raise VideoGenerationError("ElevenLabs Flows accepted the job but returned no id")

        deadline = start.timestamp() + self.config.poll_timeout
        while True:
            poll = await client.get(
                f"https://api.elevenlabs.io/v1/flows/video/{gen_id}", headers=headers
            )
            _check(poll, "ElevenLabs Flows poll")
            state = poll.json() or {}
            status = state.get("status")
            if status == "completed":
                url = state.get("content_url")
                if not url:
                    raise VideoGenerationError(
                        "ElevenLabs Flows completed but returned no content_url"
                    )
                return GeneratedVideo(
                    prompt=prompt,
                    provider=VideoProvider.ELEVENLABS,
                    url=url,
                    model=model,
                    duration_s=extra_input.get("duration_secs") if extra_input else None,
                    generation_time=(datetime.now() - start).total_seconds(),
                )
            if status == "failed":
                # Failed generations are not charged — say why it failed.
                raise VideoGenerationError(
                    "ElevenLabs Flows generation failed: "
                    f"{state.get('error_message') or state.get('failure_reason') or 'unknown'}"
                )
            if datetime.now().timestamp() > deadline:
                raise TimeoutError(f"ElevenLabs Flows generation timed out (id {gen_id})")
            await asyncio.sleep(self.config.poll_interval)

    async def _eleven_image_reference(self, source: str) -> dict[str, Any]:
        """Turn a seed image into Flows' ``inline_base64`` reference.

        Flows has no plain-URL image reference: ``asset`` needs the Pro-only
        assets API and ``generation`` points at a prior flows job, so a local
        frame (or any reachable URL) is inlined — which also sidesteps a
        provider having to fetch from somewhere it cannot reach.

        A ``source`` that is a URL is the ONE caller-controlled fetch in this
        module, so it goes through the shared SSRF guard (``navig.net.ssrf`` is
        an alias of ``navig_sdk.ssrf``, owned by the SDK this plugin runs on
        standalone): every redirect hop re-validated, private ranges blocked.
        """
        p = Path(source)
        if p.exists() and p.is_file():
            data = p.read_bytes()
            mime = mimetypes.guess_type(p.name)[0]
        else:
            from navig_sdk.ssrf import safe_fetch  # noqa: PLC0415

            resp = await safe_fetch(source)
            if resp.status_code >= 400:
                raise VideoGenerationError(
                    f"seed image fetch failed ({resp.status_code}) for {source!r}"
                )
            data = resp.content
            mime = resp.headers.get("content-type", "").split(";")[0] or None
        # The API caps an inline image at 25MB decoded — fail on OUR side of the
        # request rather than sending something it rejects with a 422.
        if len(data) > 25 * 1024 * 1024:
            raise VideoGenerationError(
                f"seed image is {len(data) // (1024 * 1024)}MB — flows inline "
                "images are capped at 25MB; shrink it or upload it as an asset"
            )
        mime = mime if mime in ("image/jpeg", "image/png", "image/webp", "image/heic", "image/heif") else "image/png"
        return {
            "type": "inline_base64",
            "content_base64": base64.b64encode(data).decode("ascii"),
            "mime_type": mime,
        }


async def generate_video(
    prompt: str,
    provider: str = "gemini_veo",
    image_url: str | None = None,
    **kwargs,
) -> GeneratedVideo:
    """Generate a single video from a prompt. Returns a GeneratedVideo."""
    config = VideoGenerationConfig.from_env()
    config.provider = VideoProvider(provider)
    gen = VideoGenerator(config)
    try:
        return await gen.generate(prompt, image_url=image_url, **kwargs)
    finally:
        await gen.close()


def is_video_generation_available() -> bool:
    """True when at least one video provider has a resolvable key."""
    if not HTTPX_AVAILABLE:
        return False
    from navig_media_core.tools.media_providers import MEDIA_CATALOG, key_status

    return any(key_status(entry) for entry in MEDIA_CATALOG["video"])
