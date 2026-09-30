"""Media generation — the NAVIG Media studio deck API.

Drives the context-aware,
iterative, versioned generation workflow (:mod:`navig.media.generation_service`).

Routes (all under /api/deck, all gated on the free ``media`` module):
  POST   /media/generate            generate variant(s) for a brief
                                    image → synchronous; video/audio → a job
  POST   /media/reroll/{id}         "next": a fresh variant in the same group
  GET    /media/jobs/{id}           poll a video/audio job {state,progress,result}
  POST   /media/{id}/keep           promote a variant into refs/<category>
  POST   /media/{id}/reject         retain a variant in refs/.rejected
  GET    /media/history             the gallery (?modality= &status=)
  GET    /media/context             what project context will be injected
  GET    /media/file/{id}/raw       serve a generated variant (preview)
  POST   /media/reference           upload a target-spot screenshot
  GET    /media/reference/{id}/raw  serve an uploaded reference

Provider catalog + key config stay on core's ``GET /api/deck/media/providers``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import uuid
from pathlib import Path
from typing import Any

try:
    from aiohttp import web
except ImportError:
    web = None

logger = logging.getLogger(__name__)

# In-process job registry for slow (video/audio) generations. Single-process
# gateway, so a module dict is sufficient; completion is also pushed over SSE.
# Finished jobs are pruned (oldest first) past _MAX_FINISHED_JOBS; running jobs
# are never pruned. Task references are held so pending jobs can't be GC'd.
_JOBS: dict[str, dict[str, Any]] = {}
_JOB_TASKS: set["asyncio.Task[None]"] = set()
_MAX_FINISHED_JOBS = 50


def _prune_finished_jobs(limit: int = _MAX_FINISHED_JOBS) -> None:
    """Cap _JOBS growth: drop the OLDEST finished jobs beyond ``limit``."""
    finished = [jid for jid, job in _JOBS.items() if job.get("state") in ("done", "failed")]
    for jid in finished[: max(0, len(finished) - limit)]:
        _JOBS.pop(jid, None)


def _ok(data: object, status: int = 200) -> "web.Response":
    return web.json_response({"ok": True, "data": data}, status=status)


def _err(msg: str, status: int = 500) -> "web.Response":
    return web.json_response({"ok": False, "error": msg}, status=status)


async def _body(request: "web.Request") -> dict[str, Any]:
    try:
        return await request.json()
    except Exception:
        return {}


def _public(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Attach a browser-previewable ``url`` to a variant row (same-origin, like Studio)."""
    if not row:
        return row
    return {**row, "url": f"/api/deck/media/file/{row['id']}/raw"}


def _public_result(result: dict[str, Any]) -> dict[str, Any]:
    """Enrich a {group_id, variants:[…]} generation result with preview urls."""
    if isinstance(result, dict) and isinstance(result.get("variants"), list):
        return {**result, "variants": [_public(v) for v in result["variants"]]}
    return result


def _reference_dir() -> Path:
    from navig.platform import paths

    d = paths.data_dir() / "media" / "reference"
    d.mkdir(parents=True, exist_ok=True)
    return d


# A reference id is minted as ``uuid4().hex`` — a bare token. It arrives back straight
# from the URL, and aiohttp's ``{id}`` excludes ``/`` but NOT ``\`` or ``.``, so a crafted
# id (``..%5C..%5Cx`` on Windows) would build a sidecar path OUTSIDE the reference dir —
# a path-traversal / arbitrary-file read. Whitelist the token shape (no separator, no dot).
_SAFE_REF_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _safe_reference_sidecar(ref_id: str) -> Path | None:
    """The ``<ref_id>.json`` sidecar path, or ``None`` if *ref_id* is unsafe.

    Rejects anything but a bare token, then belt-and-suspenders confirms the resolved
    path stays inside the reference dir (so even a loosened whitelist can't escape).
    """
    if not _SAFE_REF_ID.match(ref_id):
        return None
    ref_dir = _reference_dir()
    sidecar = ref_dir / f"{ref_id}.json"
    try:
        sidecar.resolve().relative_to(ref_dir.resolve())
    except (OSError, ValueError):
        return None
    return sidecar


async def _emit_job(job_id: str, state: str) -> None:
    try:
        from navig.gateway.system_events import get_system_events

        queue = get_system_events()
        if queue is not None:
            await queue.emit("media_job_update", {"job_id": job_id, "state": state})
    except Exception:  # noqa: BLE001
        pass


# ─── Generate ────────────────────────────────────────────────────────────────


def _gen_kwargs(body: dict[str, Any]) -> dict[str, Any]:
    palette = body.get("palette")
    if isinstance(palette, str):
        palette = [p.strip() for p in palette.split(",") if p.strip()]
    return {
        "modality": (body.get("modality") or "image").strip(),
        "prompt": (body.get("prompt") or "").strip(),
        "provider": body.get("provider") or None,
        "model": body.get("model") or None,
        "placement": body.get("placement") or None,
        "palette": palette or None,
        "reference_ref": body.get("reference_ref") or None,
        "size": body.get("size") or None,
        "kind": body.get("kind") or "music",
        "duration_s": body.get("duration_s"),
        "n": int(body.get("n") or 1),
        "seed": body.get("seed"),
    }


async def handle_media_generate(request: "web.Request") -> "web.Response":
    from navig.media import generation_service as svc

    body = await _body(request)
    try:
        kwargs = _gen_kwargs(body)
    except (TypeError, ValueError):
        return _err("'n' must be an integer", status=400)
    if not kwargs["prompt"]:
        return _err("prompt is required", status=400)
    if kwargs["modality"] not in svc.VALID_MODALITIES:
        return _err(f"modality must be one of {svc.VALID_MODALITIES}", status=400)

    # Image = fast → synchronous. Video/audio = slow → background job.
    if kwargs["modality"] == "image":
        try:
            result = await svc.generate(**kwargs)
            return _ok({"mode": "sync", **_public_result(result)})
        except Exception as exc:  # noqa: BLE001
            logger.exception("media generate (sync) failed")
            return _err(str(exc), status=502)

    return _start_job(lambda: svc.generate(**kwargs))


async def handle_media_reroll(request: "web.Request") -> "web.Response":
    from navig.media import generation_service as svc
    from navig.store.generated_media import get_generated_media

    source_id = request.match_info.get("id", "")
    body = await _body(request)
    seed = body.get("seed")
    row = get_generated_media().get(source_id)
    if row is None:
        return _err("no such variant", status=404)

    if row["modality"] == "image":
        try:
            return _ok({"mode": "sync", **_public_result(await svc.reroll(source_id, seed=seed))})
        except Exception as exc:  # noqa: BLE001
            logger.exception("media reroll (sync) failed")
            return _err(str(exc), status=502)

    return _start_job(lambda: svc.reroll(source_id, seed=seed))


def _start_job(coro_factory) -> "web.Response":
    """Kick off a background generation job; return {mode:'job', job_id}."""
    _prune_finished_jobs()
    job_id = uuid.uuid4().hex
    _JOBS[job_id] = {"state": "running", "progress": None, "result": None, "error": None}

    async def _run() -> None:
        try:
            result = await coro_factory()
            _JOBS[job_id].update(state="done", result=result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("media job failed")
            _JOBS[job_id].update(state="failed", error=str(exc))
        await _emit_job(job_id, _JOBS[job_id]["state"])

    task = asyncio.create_task(_run())
    _JOB_TASKS.add(task)  # keep a strong ref so the pending task can't be GC'd
    task.add_done_callback(_JOB_TASKS.discard)
    return _ok({"mode": "job", "job_id": job_id})


async def handle_media_job(request: "web.Request") -> "web.Response":
    job_id = request.match_info.get("id", "")
    job = _JOBS.get(job_id)
    if job is None:
        return _err("no such job", status=404)
    out = {"job_id": job_id, **job}
    if isinstance(out.get("result"), dict):
        out["result"] = _public_result(out["result"])
    return _ok(out)


# ─── Keep / reject ───────────────────────────────────────────────────────────


async def handle_media_keep(request: "web.Request") -> "web.Response":
    from navig.media import generation_service as svc

    media_id = request.match_info.get("id", "")
    try:
        row = svc.keep(media_id)
        return _ok(_public(row)) if row else _err("no such variant", status=404)
    except Exception as exc:  # noqa: BLE001
        logger.exception("media keep failed")
        return _err(str(exc))


async def handle_media_reject(request: "web.Request") -> "web.Response":
    from navig.media import generation_service as svc

    media_id = request.match_info.get("id", "")
    try:
        row = svc.reject(media_id)
        return _ok(_public(row)) if row else _err("no such variant", status=404)
    except Exception as exc:  # noqa: BLE001
        logger.exception("media reject failed")
        return _err(str(exc))


# ─── Derivative ops (edit / remove-bg / redesign / process / license) ────────


async def handle_media_edit(request: "web.Request") -> "web.Response":
    from navig.media import generation_service as svc

    media_id = request.match_info.get("id", "")
    body = await _body(request)
    instruction = (body.get("instruction") or "").strip()
    if not instruction:
        return _err("instruction is required", status=400)
    try:
        return _ok(_public(await svc.edit(media_id, instruction, mask_path=body.get("mask_path"))))
    except Exception as exc:  # noqa: BLE001
        logger.exception("media edit failed")
        return _err(str(exc), status=502)


async def handle_media_remove_bg(request: "web.Request") -> "web.Response":
    from navig.media import generation_service as svc

    media_id = request.match_info.get("id", "")
    try:
        return _ok(_public(await svc.remove_background(media_id)))
    except Exception as exc:  # noqa: BLE001
        logger.exception("media remove-bg failed")
        return _err(str(exc), status=502)


async def handle_media_redesign(request: "web.Request") -> "web.Response":
    from navig.media import generation_service as svc

    media_id = request.match_info.get("id", "")
    body = await _body(request)
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return _err("prompt is required", status=400)
    try:
        strength = float(body.get("strength") or 0.4)
    except (TypeError, ValueError):
        return _err("'strength' must be a number", status=400)
    try:
        return _ok(_public(await svc.redesign(media_id, prompt, strength=strength)))
    except Exception as exc:  # noqa: BLE001
        logger.exception("media redesign failed")
        return _err(str(exc), status=502)


async def handle_media_process(request: "web.Request") -> "web.Response":
    from navig.media import generation_service as svc

    media_id = request.match_info.get("id", "")
    body = await _body(request)
    ops = body.get("ops")
    if not isinstance(ops, list) or not ops:
        return _err("ops must be a non-empty list", status=400)
    try:
        return _ok(_public(svc.process(media_id, ops)))
    except Exception as exc:  # noqa: BLE001
        logger.exception("media process failed")
        return _err(str(exc), status=502)


async def handle_media_license(request: "web.Request") -> "web.Response":
    from navig.media import generation_service as svc

    media_id = request.match_info.get("id", "")
    body = await _body(request)
    lic = (body.get("license") or "").strip()
    if not lic:
        return _err("license is required", status=400)
    row = svc.set_license(media_id, lic)
    return _ok(_public(row)) if row else _err("no such variant", status=404)


# ─── External ingest + review aids ───────────────────────────────────────────


async def handle_media_ingest(request: "web.Request") -> "web.Response":
    from navig.media import generation_service as svc

    body = await _body(request)
    paths = body.get("paths") or body.get("dir")
    if not paths:
        return _err("paths or dir is required", status=400)
    try:
        result = svc.ingest(
            paths, provider=body.get("provider") or "external", model=body.get("model"),
            prompt=body.get("prompt") or "", seed=body.get("seed"), license=body.get("license"),
        )
        return _ok(_public_result(result))
    except Exception as exc:  # noqa: BLE001
        logger.exception("media ingest failed")
        return _err(str(exc))


async def handle_media_contact_sheet(request: "web.Request") -> "web.Response":
    from navig.media import generation_service as svc

    q = request.query
    try:
        path = svc.contact_sheet(
            modality=q.get("modality") or "image", status=q.get("status") or None,
            group_id=q.get("group_id") or None, cols=int(q.get("cols") or 6),
        )
        return _ok({"path": path})
    except Exception as exc:  # noqa: BLE001
        logger.exception("media contact sheet failed")
        return _err(str(exc))


async def handle_media_palette(request: "web.Request") -> "web.Response":
    from navig.media import generation_service as svc

    q = request.query
    source = q.get("source")
    if not source:
        return _err("source (dir) is required", status=400)
    try:
        return _ok({"palette": svc.extract_palette(source, colors=int(q.get("colors") or 16))})
    except Exception as exc:  # noqa: BLE001
        logger.exception("media palette failed")
        return _err(str(exc))


# ─── History / context ───────────────────────────────────────────────────────


async def handle_media_history(request: "web.Request") -> "web.Response":
    from navig.media import generation_service as svc

    modality = request.query.get("modality") or None
    status = request.query.get("status") or None
    all_spaces = request.query.get("all", "").lower() in ("1", "true", "yes")
    try:
        rows = [_public(v) for v in svc.history(modality=modality, status=status,
                                                all_spaces=all_spaces)]
        return _ok({"variants": rows})
    except Exception as exc:  # noqa: BLE001
        logger.exception("media history failed")
        return _err(str(exc))


async def handle_media_context(request: "web.Request") -> "web.Response":
    """Preview which project context (palette + style note) will be injected."""
    from navig.media import context_builder

    try:
        built = context_builder.build_context("")
        return _ok(built["context"])
    except Exception as exc:  # noqa: BLE001
        logger.exception("media context failed")
        return _err(str(exc))


# ─── File serving (generated variants + references) ──────────────────────────


async def handle_media_file_raw(request: "web.Request") -> "web.Response":
    from navig.store.generated_media import get_generated_media

    media_id = request.match_info.get("id", "")
    row = get_generated_media().get(media_id)
    if row is None or not row.get("path"):
        return _err("media not found", status=404)
    path = Path(row["path"])
    if not path.exists():
        return _err("media file missing", status=404)
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return web.FileResponse(path, headers={"Content-Type": ctype})


async def handle_media_reference_upload(request: "web.Request") -> "web.Response":
    try:
        post = await request.post()
        field = post.get("file")
        if field is None or not hasattr(field, "file"):
            return _err("no file field", status=400)
        filename = getattr(field, "filename", None) or "reference"
        mime = getattr(field, "content_type", None) or "application/octet-stream"
        data = field.file.read()
        if not data:
            return _err("empty file", status=400)
        ref_id = uuid.uuid4().hex
        ext = Path(filename).suffix or ".png"
        path = _reference_dir() / f"{ref_id}{ext}"
        path.write_bytes(data)
        meta = {"reference_id": ref_id, "filename": filename, "mime": mime,
                "size": len(data), "path": str(path)}
        (_reference_dir() / f"{ref_id}.json").write_text(json.dumps(meta), encoding="utf-8")
        return _ok({"reference_id": ref_id, "url": f"/api/deck/media/reference/{ref_id}/raw",
                    "kind": "photo"})
    except Exception as exc:  # noqa: BLE001
        logger.exception("media reference upload failed")
        return _err(str(exc))


async def handle_media_reference_raw(request: "web.Request") -> "web.Response":
    ref_id = request.match_info.get("id", "")
    sidecar = _safe_reference_sidecar(ref_id)
    if sidecar is None or not sidecar.exists():
        return _err("reference not found", status=404)
    try:
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        path = Path(meta["path"])
        if not path.exists():
            return _err("reference file missing", status=404)
        return web.FileResponse(path, headers={"Content-Type": meta.get("mime", "image/png")})
    except Exception as exc:  # noqa: BLE001
        logger.exception("media reference raw failed")
        return _err(str(exc))


# ─── Registration ────────────────────────────────────────────────────────────


def register(app: "web.Application") -> None:
    """Mount the media generation routes, each gated on the free ``media`` module."""
    from navig.modules.gate import requires_module

    g = requires_module("generate")
    app.router.add_post("/api/deck/media/generate", g(handle_media_generate))
    app.router.add_post("/api/deck/media/reroll/{id}", g(handle_media_reroll))
    app.router.add_get("/api/deck/media/jobs/{id}", g(handle_media_job))
    app.router.add_post("/api/deck/media/{id}/keep", g(handle_media_keep))
    app.router.add_post("/api/deck/media/{id}/reject", g(handle_media_reject))
    app.router.add_post("/api/deck/media/{id}/edit", g(handle_media_edit))
    app.router.add_post("/api/deck/media/{id}/remove-bg", g(handle_media_remove_bg))
    app.router.add_post("/api/deck/media/{id}/redesign", g(handle_media_redesign))
    app.router.add_post("/api/deck/media/{id}/process", g(handle_media_process))
    app.router.add_post("/api/deck/media/{id}/license", g(handle_media_license))
    app.router.add_post("/api/deck/media/ingest", g(handle_media_ingest))
    app.router.add_get("/api/deck/media/contact-sheet", g(handle_media_contact_sheet))
    app.router.add_get("/api/deck/media/palette", g(handle_media_palette))
    app.router.add_get("/api/deck/media/history", g(handle_media_history))
    app.router.add_get("/api/deck/media/context", g(handle_media_context))
    app.router.add_get("/api/deck/media/file/{id}/raw", g(handle_media_file_raw))
    app.router.add_post("/api/deck/media/reference", g(handle_media_reference_upload))
    app.router.add_get("/api/deck/media/reference/{id}/raw", g(handle_media_reference_raw))
