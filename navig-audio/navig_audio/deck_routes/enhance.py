"""Deck routes for Audio Enhance — NAVIG OS → Media Studio → Enhance.

    GET  /api/deck/audio/enhance/presets          enhancement presets, the default, modes, engines
    POST /api/deck/audio/enhance/upload           multipart ``file`` → a new audio variant (drag & drop)
    POST /api/deck/media/{id}/analyze             findings + measurements (nothing is written)
    GET  /api/deck/media/{id}/waveform?points=N   min/max peaks for drawing (N ≤ 2000)
    POST /api/deck/media/{id}/enhance             {mode?, preset_id?, params?, allow_cloud?} → job
    GET  /api/deck/audio/enhance/jobs/{job}       state · progress · message · result | error
    POST /api/deck/audio/enhance/jobs/{job}/cancel
    POST /api/deck/media/{id}/export              {format: wav|wav16|flac|mp3, dir?} → a file on disk

A finished enhancement is a NEW variant in the same group (``op: enhance``), never a
change to the source; its report, plan and verification ride in the variant's context.
Accept / discard use the existing ``/media/{id}/keep`` and ``/reject``. Every route is
gated on the free ``audio`` module. Responses: ``{"ok": true, "data": …}`` or
``{"ok": false, "error": …}``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aiohttp import web

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 300_000_000
_SAFE_JOB = re.compile(r"^[0-9a-f]{32}$")
_JOBS: dict[str, dict[str, Any]] = {}
_TASKS: set[asyncio.Task[None]] = set()
_MAX_FINISHED = 30


def _ok(data: Any, status: int = 200) -> web.Response:
    from aiohttp import web

    return web.json_response({"ok": True, "data": data}, status=status)


def _err(msg: str, status: int = 400) -> web.Response:
    from aiohttp import web

    return web.json_response({"ok": False, "error": msg}, status=status)


async def _json(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed JSON is an empty body, validated below
        return {}
    return body if isinstance(body, dict) else {}


def _row(media_id: str) -> dict[str, Any] | None:
    from navig_media_core.generated_media import get_generated_media

    return get_generated_media().get(media_id)


def _audio_path(media_id: str) -> tuple[dict[str, Any] | None, Path | None, str | None]:
    row = _row(media_id)
    if row is None:
        return None, None, "no such variant"
    if row.get("modality") != "audio":
        return row, None, "enhance only applies to audio"
    path = Path(row.get("path") or "")
    if not path.is_file():
        return row, None, "the audio file for this variant is missing"
    return row, path, None


def _public(row: dict[str, Any] | None) -> dict[str, Any] | None:
    return {**row, "url": f"/api/deck/media/file/{row['id']}/raw"} if row else row


async def _emit(job_id: str) -> None:
    try:
        from navig.gateway.system_events import get_system_events

        queue = get_system_events()
        if queue is not None:
            job = _JOBS.get(job_id, {})
            await queue.emit("audio_enhance_update", {
                "job_id": job_id, "state": job.get("state"), "progress": job.get("progress"),
                "message": job.get("message"),
            })
    except Exception:  # noqa: BLE001 — SSE is a nicety; the job route is the truth
        logger.debug("audio enhance SSE emit failed", exc_info=True)


def _prune() -> None:
    done = [j for j, v in _JOBS.items() if v.get("state") in ("done", "failed", "cancelled")]
    for j in done[: max(0, len(done) - _MAX_FINISHED)]:
        _JOBS.pop(j, None)


# ── handlers ─────────────────────────────────────────────────────────


async def handle_presets(request: web.Request) -> web.Response:
    from navig_audio.enhance import MODES
    from navig_audio.enhance import presets as presets_mod
    from navig_audio.enhance.engine import FORMATS, available_engines

    def _run():
        return {"presets": presets_mod.list_presets(), "default": presets_mod.default_id(),
                "modes": list(MODES), "formats": list(FORMATS), "engines": available_engines()}

    return _ok(await asyncio.to_thread(_run))


async def handle_upload(request: web.Request) -> web.Response:
    from navig_generate.media import generation_service as svc

    if (request.content_length or 0) > MAX_UPLOAD_BYTES:
        return _err(f"file is larger than {MAX_UPLOAD_BYTES // 1_000_000} MB", 413)
    try:
        post = await request.post()
    except Exception:  # noqa: BLE001
        return _err("expected a multipart upload with a 'file' field")
    field = post.get("file")
    if field is None or not hasattr(field, "file"):
        return _err("no file field")
    # Only the extension of the given name is used; the stored name is minted.
    filename = Path(str(getattr(field, "filename", "") or "audio")).name[:200]
    if Path(filename).suffix.lower() not in svc.AUDIO_EXTS:
        return _err(f"{filename}: not an audio file NAVIG reads ({', '.join(sorted(svc.AUDIO_EXTS))})", 415)
    data = field.file.read()
    if not data:
        return _err("empty file")

    def _store() -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="navig-upload-") as tmp:
            p = Path(tmp) / f"upload{Path(filename).suffix.lower()}"
            p.write_bytes(data)
            from navig_audio.enhance.analysis import AnalyzeError, probe

            try:
                probe(p)  # refuse non-audio content before it enters the library
            except AnalyzeError as exc:
                raise ValueError(str(exc)) from exc
            return svc.import_audio(p, filename=filename)

    try:
        row = await asyncio.to_thread(_store)
    except ValueError as exc:
        return _err(str(exc), 415)
    return _ok(_public(row), 201)


async def handle_analyze(request: web.Request) -> web.Response:
    from navig_audio.enhance import AnalyzeError, analyze

    _, path, err = _audio_path(request.match_info.get("id", ""))
    if err:
        return _err(err, 404 if err == "no such variant" else 400)
    body = await _json(request)
    try:
        report = await asyncio.to_thread(analyze, path, category=str(body.get("category") or "auto"))
    except AnalyzeError as exc:
        return _err(str(exc), 422)
    return _ok(report.to_dict())


async def handle_waveform(request: web.Request) -> web.Response:
    from navig_audio.enhance.analysis import AnalyzeError, peaks

    _, path, err = _audio_path(request.match_info.get("id", ""))
    if err:
        return _err(err, 404 if err == "no such variant" else 400)
    try:
        points = max(50, min(2000, int(request.query.get("points", "600"))))
    except ValueError:
        return _err("points must be a number")
    try:
        data = await asyncio.to_thread(peaks, path, points)
    except AnalyzeError as exc:
        return _err(str(exc), 422)
    return _ok(data)


async def handle_enhance(request: web.Request) -> web.Response:
    from navig_audio.enhance import presets as presets_mod

    media_id = request.match_info.get("id", "")
    row, path, err = _audio_path(media_id)
    if err:
        return _err(err, 404 if err == "no such variant" else 400)
    body = await _json(request)
    mode = body.get("mode")
    preset_id = str(body.get("preset_id") or "") or (None if mode else presets_mod.default_id())
    params: dict[str, Any] = {}
    if preset_id:
        try:
            params = presets_mod.load(preset_id)
        except KeyError:
            return _err(f"no enhancement preset {preset_id!r}", 400)
    extra = body.get("params") if isinstance(body.get("params"), dict) else {}
    if extra:
        try:
            from navig.presets.model import validate_params

            extra = validate_params("audio.enhance", extra)
        except ImportError:
            pass
        except ValueError as exc:
            return _err(str(exc), 400)
    params.update(extra)
    # Cloud steps need the caller to say so for THIS request (the UI shows provider,
    # cost and the upload first). A preset can ask for a cloud engine; it can't grant it.
    params["allow_cloud"] = bool(body.get("allow_cloud"))
    run_mode = str(mode or params.get("mode") or "preserve")

    _prune()
    job_id = uuid.uuid4().hex
    cancel = threading.Event()
    _JOBS[job_id] = {"state": "running", "progress": 0.0, "message": "queued", "media_id": media_id,
                     "mode": run_mode, "preset_id": preset_id, "started": time.time(), "_cancel": cancel}
    loop = asyncio.get_running_loop()

    def _progress(msg: str, frac: float) -> None:
        job = _JOBS.get(job_id)
        if job is not None:
            job.update(progress=round(frac, 2), message=msg)
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_emit(job_id)))

    def _work() -> dict[str, Any]:
        from navig_audio.enhance import enhance
        from navig_generate.media import generation_service as svc

        with tempfile.TemporaryDirectory(prefix="navig-enhance-out-") as tmp:
            res = enhance(path, mode=run_mode, params=params, out_dir=Path(tmp), formats=["wav"],
                          preset_id=preset_id, cancel=cancel, progress=_progress)
            out: dict[str, Any] = {"verdict": res.verdict, "report": res.report.to_dict(),
                                   "plan": res.plan.to_dict(), "attempts": [a.__dict__ for a in res.attempts],
                                   "verification": res.verification.to_dict() if res.verification else None,
                                   "cost": res.cost, "variant": None}
            if res.outputs:
                variant = svc.persist_derivative(
                    media_id, res.outputs[0], op="enhance", ext=".wav",
                    provider="elevenlabs+ffmpeg" if res.plan.cloud else "ffmpeg",
                    model=f"enhance:{run_mode}",
                    params={"mode": run_mode, "preset_id": preset_id, "settings": params},
                    context={"enhance": {"verdict": res.verdict, "plan": out["plan"],
                                         "verification": out["verification"],
                                         "source_sha256": res.source_sha256}},
                )
                out["variant"] = _public(variant)
            return out

    async def _run() -> None:
        try:
            result = await asyncio.to_thread(_work)
            _JOBS[job_id].update(state="done", result=result, progress=1.0, message="done")
        except Exception as exc:  # noqa: BLE001 — the job record carries the error to the UI
            from navig_audio.enhance import Cancelled

            if isinstance(exc, Cancelled):
                _JOBS[job_id].update(state="cancelled", message="cancelled")
            else:
                logger.warning("audio enhance job failed: %s", exc)
                _JOBS[job_id].update(state="failed", error=str(exc), message="failed")
        await _emit(job_id)

    task = asyncio.create_task(_run())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return _ok({"job_id": job_id, "mode": run_mode, "preset_id": preset_id}, 202)


async def handle_regenerate(request: web.Request) -> web.Response:
    """POST /media/{id}/regenerate — AI regeneration as a job; each take is a NEW variant.

    Body: ``{op, allow_cloud, voice_id?, direction?, count?, stem?, replace_with?,
    describe_with_ai?, dry_run?}``. ``allow_cloud`` must be true for a real run (the UI
    shows what is uploaded and billed first). ``dry_run`` answers the plan synchronously.
    """
    from navig_audio.regenerate import OPS, RegenerateError, regenerate

    media_id = request.match_info.get("id", "")
    row, path, err = _audio_path(media_id)
    if err:
        return _err(err, 404 if err == "no such variant" else 400)
    body = await _json(request)
    op = str(body.get("op") or "")
    if op not in OPS:
        return _err(f"op must be one of {', '.join(OPS)}")
    kwargs = {
        "op": op, "allow_cloud": bool(body.get("allow_cloud")), "voice_id": body.get("voice_id") or None,
        "direction": str(body.get("direction") or ""), "stem": body.get("stem") or None,
        "replace_with": str(body.get("replace_with") or ""),
        "use_ai_description": bool(body.get("describe_with_ai")),
    }
    try:
        kwargs["count"] = max(1, min(4, int(body.get("count") or 2)))
    except (TypeError, ValueError):
        return _err("count must be a number")
    if body.get("dry_run"):
        try:
            res = await asyncio.to_thread(regenerate, path, dry_run=True, **kwargs)
        except RegenerateError as exc:
            return _err(str(exc))
        return _ok(res.to_dict())
    if not kwargs["allow_cloud"]:
        return _err("regeneration runs on ElevenLabs — confirm the upload/billing (allow_cloud) first", 400)

    _prune()
    job_id = uuid.uuid4().hex
    _JOBS[job_id] = {"state": "running", "progress": 0.0, "message": "queued", "media_id": media_id,
                     "kind": "regenerate", "op": op, "started": time.time(), "_cancel": threading.Event()}
    loop = asyncio.get_running_loop()

    def _progress(msg: str, frac: float) -> None:
        job = _JOBS.get(job_id)
        if job is not None:
            job.update(progress=round(frac, 2), message=msg)
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_emit(job_id)))

    def _work() -> dict[str, Any]:
        from navig_media_core.media import generation_service as svc

        with tempfile.TemporaryDirectory(prefix="navig-regen-out-") as tmp:
            res = regenerate(path, out_dir=Path(tmp), progress=_progress, **kwargs)
            out = res.to_dict()
            variants = []
            for take in res.takes:
                variants.append(_public(svc.persist_derivative(
                    media_id, take.path, op=f"regenerate:{op}", ext=".wav", provider="elevenlabs",
                    model=f"regenerate:{op}",
                    params={k: v for k, v in kwargs.items() if k != "allow_cloud"},
                    context={"generated": True, "regenerate": {"op": op, "prompt": take.prompt,
                             "similarity": take.similarity, "plan": res.plan,
                             "description": out["description"], "source_sha256": res.source_sha256}},
                )))
            out["variants"] = variants
            return out

    async def _run_job() -> None:
        try:
            result = await asyncio.to_thread(_work)
            _JOBS[job_id].update(state="done", result=result, progress=1.0, message="done")
        except Exception as exc:  # noqa: BLE001 — the job record carries the error to the UI
            logger.warning("audio regenerate job failed: %s", exc)
            _JOBS[job_id].update(state="failed", error=str(exc), message="failed")
        await _emit(job_id)

    task = asyncio.create_task(_run_job())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return _ok({"job_id": job_id, "op": op}, 202)


async def handle_voices(request: web.Request) -> web.Response:
    """GET /audio/voices — the account's ElevenLabs voices (for voice / replace-vocals)."""
    try:
        from navig_generate.tools.audio_generation import AudioGenerationConfig, AudioGenerator

        gen = AudioGenerator(AudioGenerationConfig(save_locally=False))
        try:
            voices = await gen.list_voices()
        finally:
            await gen.close()
    except Exception as exc:  # noqa: BLE001 — no key / network: say so, don't 500
        return _err(f"couldn't list ElevenLabs voices: {exc}", 503)
    return _ok([{"id": v.get("voice_id"), "name": v.get("name"), "category": v.get("category")} for v in voices])


def _job_view(job_id: str, job: dict[str, Any]) -> dict[str, Any]:
    return {"job_id": job_id, **{k: v for k, v in job.items() if not k.startswith("_")}}


async def handle_job(request: web.Request) -> web.Response:
    job_id = request.match_info.get("job", "")
    job = _JOBS.get(job_id) if _SAFE_JOB.match(job_id) else None
    if job is None:
        return _err("no such job", 404)
    return _ok(_job_view(job_id, job))


async def handle_cancel(request: web.Request) -> web.Response:
    job_id = request.match_info.get("job", "")
    job = _JOBS.get(job_id) if _SAFE_JOB.match(job_id) else None
    if job is None:
        return _err("no such job", 404)
    if job.get("state") == "running":
        job["_cancel"].set()
        job["message"] = "cancelling"
    return _ok(_job_view(job_id, job))


async def handle_export(request: web.Request) -> web.Response:
    from navig_audio.enhance.engine import FORMATS, EnhanceError, export

    media_id = request.match_info.get("id", "")
    _, path, err = _audio_path(media_id)
    if err:
        return _err(err, 404 if err == "no such variant" else 400)
    body = await _json(request)
    fmt = str(body.get("format") or "wav")
    if fmt not in FORMATS:
        return _err(f"format must be one of {', '.join(FORMATS)}")
    raw_dir = str(body.get("dir") or "").strip()
    if raw_dir:
        dest = Path(raw_dir).expanduser()
        if not dest.is_absolute() or not dest.is_dir():
            return _err("dir must be an existing folder (absolute path)")
    else:
        from navig.platform import paths

        dest = paths.data_dir() / "media" / "exports"
        dest.mkdir(parents=True, exist_ok=True)
    row = _row(media_id) or {}
    name = Path(str((row.get("context") or {}).get("filename") or path.name))
    stem_src = path.with_name(Path(name).stem + path.suffix)

    def _run() -> list[str]:
        return [str(p) for p in export(path, stem_src, dest, [fmt])]

    try:
        files = await asyncio.to_thread(_run)
    except EnhanceError as exc:
        return _err(str(exc), 500)
    return _ok({"files": files, "format": fmt, "lossy_note": fmt == "mp3"})


def register(app: web.Application) -> None:
    from navig.modules.gate import requires_module

    g = requires_module("audio")
    app.router.add_get("/api/deck/audio/enhance/presets", g(handle_presets))
    app.router.add_post("/api/deck/audio/enhance/upload", g(handle_upload))
    app.router.add_get("/api/deck/audio/enhance/jobs/{job}", g(handle_job))
    app.router.add_post("/api/deck/audio/enhance/jobs/{job}/cancel", g(handle_cancel))
    app.router.add_post("/api/deck/media/{id}/analyze", g(handle_analyze))
    app.router.add_get("/api/deck/media/{id}/waveform", g(handle_waveform))
    app.router.add_post("/api/deck/media/{id}/enhance", g(handle_enhance))
    app.router.add_post("/api/deck/media/{id}/export", g(handle_export))
    app.router.add_post("/api/deck/media/{id}/regenerate", g(handle_regenerate))
    app.router.add_get("/api/deck/audio/voices", g(handle_voices))
