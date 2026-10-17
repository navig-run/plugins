"""Studio — social composer & scheduler deck API.

Routes (all under /api/deck):
  GET    /studio/networks                  connected + available publish targets
  POST   /studio/media                     multipart upload → {media_id, path, url, kind}
  GET    /studio/media/{id}/raw            serve an uploaded media file (preview)
  GET    /studio/posts                     list posts (?status=&campaign=&content_id=)
  POST   /studio/posts                     create a draft / scheduled post; with an
                                           ``external`` block: idempotent contract
                                           submission (G6) → SOCIAL-CONTRACT §2 envelope
  GET    /studio/posts/{id}                get one post
  PATCH  /studio/posts/{id}                update a post (content/targets → rehash,
                                           version bump, approval invalidation;
                                           409 immutable on terminal statuses)
  DELETE /studio/posts/{id}                delete a post
  POST   /studio/posts/{id}/publish        publish now (idempotency_key or
                                           Idempotency-Key header → replay-safe;
                                           410 on expiry)
  POST   /studio/posts/{id}/approve        bind approval to the exact content hash (G1)
  POST   /studio/posts/{id}/reject         record an operator rejection (G1)
  GET    /social/receipts                  publish-receipt ledger (?campaign=&content_id=&network=)
  POST   /studio/ai                        AI assist (draft/rewrite/shorten/hashtags/variants)

Errors keep ``error`` as a string (deck compat) and add machine-readable
``code``/``details``. The contract behind ``external``: SOCIAL-CONTRACT.md.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from aiohttp import web
except ImportError:
    web = None

logger = logging.getLogger(__name__)

# Messaging networks (1:1 adapters that double as publish targets).
_MESSAGING = [
    {"network": "telegram", "label": "Telegram", "char_limit": 4096, "media": True},
    {"network": "discord", "label": "Discord", "char_limit": 2000, "media": True},
    {"network": "whatsapp", "label": "WhatsApp", "char_limit": 4096, "media": True},
    {"network": "sms", "label": "SMS", "char_limit": 1600, "media": False},
]

# Terminal publish statuses — content edits are refused (409 immutable).
_TERMINAL = ("publishing", "published", "partial", "failed")
# Phase-1 channel gate (SOCIAL-CONTRACT): no link, no sign-up ask in a caption.
_PHASE1_BANNED = ("http://", "https://", "sign up", "signup")


def _ok(data: object, status: int = 200) -> "web.Response":
    return web.json_response({"ok": True, "data": data}, status=status)


def _err(msg: str, status: int = 500, *, code: str | None = None,
         details: Any = None) -> "web.Response":
    payload: dict[str, Any] = {"ok": False, "error": msg}
    if code:
        payload["code"] = code
    if details is not None:
        payload["details"] = details
    return web.json_response(payload, status=status)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _body(request: "web.Request") -> dict[str, Any]:
    try:
        return await request.json()
    except Exception:
        return {}


def _store():
    from navig.store.scheduled_posts import get_scheduled_posts

    return get_scheduled_posts()


def _media_dir() -> Path:
    from navig.platform import paths

    d = paths.data_dir() / "studio" / "media"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _kind_from_mime(mime: str, filename: str) -> str:
    mime = (mime or "").lower()
    if mime.startswith("image/"):
        return "photo"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "voice" if filename.lower().endswith(".ogg") else "audio"
    return "document"


# ─── Contract submission (G6 / SOCIAL-CONTRACT) ─────────────────────────────


def _contract_of(post: dict[str, Any]) -> dict[str, Any]:
    c = post.get("content") or {}
    return c.get("contract") if isinstance(c.get("contract"), dict) else {}


def _hash_of(content: dict[str, Any], targets: list[Any]) -> str:
    from navig_social.social.content_hash import of_post

    return of_post(content, targets)


def _validate_external(content: dict[str, Any], targets: list[Any],
                       body_text: str) -> tuple[list[str], list[str]]:
    """Validate an incoming contract submission → (errors, warnings).

    Errors refuse the write (422); warnings ride the envelope. Contract paths
    are workspace-relative to the producer, so media existence is only advisory
    here — SCHEMA's own builder fail-closes on its side.
    """
    errors: list[str] = []
    warnings: list[str] = []
    contract = content.get("contract") if isinstance(content.get("contract"), dict) else {}
    canonical = body_text
    if contract:
        canonical = str(((contract.get("captions") or {}).get("canonical")) or body_text)
        for token in _PHASE1_BANNED:
            if token in canonical.lower():
                errors.append(f"phase-1 gate: caption contains {token!r} (no links, no signup)")
        dest_nets = {d.get("network") for d in (contract.get("destinations") or [])
                     if isinstance(d, dict)}
        tgt_nets = {t.get("network") for t in targets if isinstance(t, dict)}
        if dest_nets and not tgt_nets <= dest_nets:
            errors.append(
                f"targets {sorted(tgt_nets - dest_nets)} not among contract destinations "
                f"{sorted(dest_nets)} — the contract is the authority for destinations"
            )
        for m in contract.get("media") or []:
            if not isinstance(m, dict) or m.get("state") != "built":
                continue
            p = Path(m.get("path") or "")
            if p.is_absolute() and not p.is_file():
                warnings.append(f"built media not found at {p} (publish may fail)")
            elif not p.is_absolute():
                warnings.append(f"media path {p} is workspace-relative — not verified here")
    if not canonical.strip():
        errors.append("caption/body is empty")
    for t in targets:
        if not isinstance(t, dict) or not (t.get("network") or "").strip():
            errors.append("each target needs a network")
            break
    return errors, warnings


def _project_external(body: dict[str, Any]) -> dict[str, Any]:
    """Derive the stored shape from an incoming submission.

    The contract is authoritative: ``content.content_id``/``content_hash`` are
    written from the ``external`` block (their Stage-C passthrough — ``of_post``
    then returns the pinned hash verbatim), body comes from
    ``captions.canonical``, ``per_network`` from ``captions.platforms``, and
    ``approval_policy`` from the explicit field or the contract's
    ``approval.required``.
    """
    ext = dict(body.get("external") or {})
    content = dict(body.get("content") or {})
    contract = content.get("contract") if isinstance(content.get("contract"), dict) else {}
    text = body.get("body") or ""
    if contract:
        text = str(((contract.get("captions") or {}).get("canonical")) or text)
        content["body"] = text
        if not content.get("per_network"):
            platforms = (contract.get("captions") or {}).get("platforms") or {}
            content["per_network"] = {net: {"body": cap} for net, cap in platforms.items()}
    if ext.get("content_id"):
        content["content_id"] = str(ext["content_id"])
    if ext.get("content_hash"):
        content["content_hash"] = str(ext["content_hash"])  # §3 passthrough
    policy = ext.get("approval_policy")
    if not policy:
        required = bool((contract.get("approval") or {}).get("required")) if contract else False
        policy = "required" if required else "auto"
    content["approval_policy"] = policy
    return {"body": text, "content": content,
            "targets": list(body.get("targets") or []), "policy": policy,
            "campaign": ext.get("campaign_id") or content.get("campaign_id")}


def _envelope(post: dict[str, Any], acceptance: str,
              errors: list[str] | None = None, warnings: list[str] | None = None) -> dict[str, Any]:
    """The NAVIG → producer envelope (SOCIAL-CONTRACT §2, Stage-B shape)."""
    receipts = post.get("receipts") or []
    by_key = {(r.get("network"), r.get("target")): r for r in receipts}
    networks: list[dict[str, Any]] = []
    for t in post.get("targets") or []:
        net, tgt = t.get("network"), t.get("target")
        r = by_key.get((net, tgt))
        if r is not None:
            state = "published" if r.get("ok") else "failed"
            url, error = r.get("url"), r.get("error")
        elif post.get("status") in ("failed", "partial"):
            state, url, error = "failed", None, post.get("last_error")
        elif post.get("status") == "expired":
            state, url, error = "failed", None, post.get("last_error") or "expired"
        else:
            state, url, error = "queued", None, None
        networks.append({"network": net, "target": tgt, "state": state,
                         "url": url, "error": error, "receipt_id": r.get("id") if r else None})
    contract = _contract_of(post)
    manual = [
        {"network": d.get("network"), "account": d.get("account"),
         "connect_state": d.get("connect_state")}
        for d in (contract.get("destinations") or [])
        if isinstance(d, dict) and d.get("publish_mode") == "manual_handoff"
    ]
    return {
        "acceptance": acceptance,
        "content_id": post.get("content_id"),
        "post_id": post.get("id"),
        "version": {"id": post.get("version") or 1, "content_hash": post.get("content_hash")},
        "validation": {"ok": not (errors or []), "errors": errors or [], "warnings": warnings or []},
        "approval": {"state": post.get("approval_state") or "none",
                     "approved_hash": post.get("approved_hash"),
                     "approved_at": post.get("approved_at")},
        "networks": networks,
        "receipts": receipts,
        "manual_handoff": manual,
        "post": post,
        "updated_at": post.get("updated_at"),
    }


# ─── Networks ────────────────────────────────────────────────────────────────


async def handle_studio_networks(request: "web.Request") -> "web.Response":
    """List publish targets: messaging adapters + social publishers + TG channels."""
    try:
        from navig.messaging.adapter_registry import get_adapter_registry
        from navig_social.social.registry import get_publisher_registry

        out: list[dict[str, Any]] = []

        areg = get_adapter_registry()
        for m in _MESSAGING:
            connected = False
            try:
                connected = areg.is_available(m["network"])
            except Exception:  # noqa: BLE001
                connected = False
            targets: list[dict[str, str]] = []
            if m["network"] == "telegram":
                targets = _telegram_targets()
            out.append({
                "network": m["network"], "label": m["label"], "group": "messaging",
                "connected": connected, "capabilities": {"char_limit": m["char_limit"], "media": m["media"]},
                "targets": targets,
            })

        preg = get_publisher_registry()
        _messaging_names = {m["network"] for m in _MESSAGING}
        for pub in preg.all():
            if pub.name in _messaging_names:
                continue  # already listed as a messaging network (e.g. telegram → live channel)
            connected = False
            try:
                connected = pub.is_configured()
            except Exception:  # noqa: BLE001
                connected = False
            # Brand-cased label from the publisher (LinkedIn / YouTube / Dev.to);
            # duck-typed publishers without one fall back to a titled name.
            label = getattr(pub, "display_label", "") or pub.name.title()
            # Several connected accounts → one target each, so the composer shows a chip
            # per account ("YouTube: Studio", "YouTube: Personal"). One account stays a
            # single chip, as before.
            targets: list[dict[str, Any]] = []
            if connected and getattr(pub, "multi_account", False):
                try:
                    found = await pub.list_targets()
                except Exception as exc:  # noqa: BLE001 — the network still lists
                    logger.debug("list_targets(%s) failed: %s", pub.name, exc)
                    found = []
                if len(found) > 1:
                    targets = [{"target": t.target, "display": t.display} for t in found]
            out.append({
                "network": pub.name, "label": label, "group": "publishing",
                "connected": connected, "capabilities": pub.capabilities, "targets": targets,
            })

        return _ok({"networks": out})
    except Exception as exc:
        logger.exception("studio networks failed")
        return _err(str(exc))


def _telegram_targets() -> list[dict[str, str]]:
    """Catalogued Telegram channels/groups the bot can post to."""
    try:
        from navig.store.telegram_catalog import get_telegram_catalog

        rooms = get_telegram_catalog().list_rooms()
        return [
            {"target": str(r["chat_id"]), "display": r.get("title") or str(r["chat_id"])}
            for r in rooms
            if r.get("type") in ("channel", "supergroup", "group")
        ]
    except Exception:  # noqa: BLE001
        return []


# ─── Media upload ────────────────────────────────────────────────────────────


async def handle_studio_media_upload(request: "web.Request") -> "web.Response":
    try:
        post = await request.post()
        field = post.get("file")
        if field is None or not hasattr(field, "file"):
            return _err("no file field", status=400)
        filename = getattr(field, "filename", None) or "upload"
        mime = getattr(field, "content_type", None) or "application/octet-stream"
        data = field.file.read()
        if not data:
            return _err("empty file", status=400)

        media_id = uuid.uuid4().hex
        ext = Path(filename).suffix
        path = _media_dir() / f"{media_id}{ext}"
        path.write_bytes(data)
        kind = _kind_from_mime(mime, filename)
        meta = {"media_id": media_id, "filename": filename, "mime": mime, "kind": kind,
                "size": len(data), "path": str(path)}
        (_media_dir() / f"{media_id}.json").write_text(json.dumps(meta), encoding="utf-8")
        meta["url"] = f"/api/deck/studio/media/{media_id}/raw"
        return _ok(meta)
    except Exception as exc:
        logger.exception("studio media upload failed")
        return _err(str(exc))


async def handle_studio_media_raw(request: "web.Request") -> "web.Response":
    media_id = request.match_info.get("id", "")
    sidecar = _media_dir() / f"{media_id}.json"
    if not sidecar.exists():
        return _err("media not found", status=404)
    try:
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        path = Path(meta["path"])
        if not path.exists():
            return _err("media file missing", status=404)
        return web.FileResponse(path, headers={"Content-Type": meta.get("mime", "application/octet-stream")})
    except Exception as exc:
        logger.exception("studio media raw failed")
        return _err(str(exc))


# ─── Posts CRUD ──────────────────────────────────────────────────────────────


async def handle_studio_posts_list(request: "web.Request") -> "web.Response":
    try:
        status = request.query.get("status") or None
        campaign = request.query.get("campaign") or None
        content_id = request.query.get("content_id") or None
        return _ok({"posts": _store().list(status=status, campaign=campaign,
                                           content_id=content_id)})
    except Exception as exc:
        logger.exception("studio posts list failed")
        return _err(str(exc))


async def handle_studio_post_create(request: "web.Request") -> "web.Response":
    body = await _body(request)
    ext = body.get("external")
    try:
        if isinstance(ext, dict) and ext:
            return _create_external(body, ext)
        content = body.get("content") or {"body": body.get("body", "")}
        pid = _store().create(
            body=body.get("body", "") or content.get("body", ""),
            content=content,
            targets=body.get("targets") or [],
            status=body.get("status") or "draft",
            schedule_kind=body.get("schedule_kind") or "now",
            run_at=body.get("run_at"),
            cron_expr=body.get("cron_expr"),
            campaign=body.get("campaign"),
            expires_at=body.get("expires_at"),
        )
        return _ok({"post": _store().get(pid)}, status=201)
    except Exception as exc:
        logger.exception("studio post create failed")
        return _err(str(exc))


def _create_external(body: dict[str, Any], ext: dict[str, Any]) -> "web.Response":
    """Idempotent contract submission (G6 — SOCIAL-CONTRACT §4/§6).

    Same ``content_id``: same hash → ``replay`` (nothing touched); different
    hash → ``set_content`` versions it and invalidates any approval, or 409
    once the post is terminal. Never creates a duplicate row for a retried
    submit. The response is always the §2 envelope.
    """
    store = _store()
    projected = _project_external(body)
    content_id = (projected["content"].get("content_id") or "").strip()
    if not content_id:
        return _err("validation failed", 422, code="validation_failed",
                    details={"validation": {"errors": ["external.content_id is required"],
                                            "warnings": []}})
    errors, warnings = _validate_external(projected["content"], projected["targets"],
                                          projected["body"])
    if errors:
        return _err("validation failed", 422, code="validation_failed",
                    details={"validation": {"errors": errors, "warnings": warnings}})

    incoming_hash = _hash_of(projected["content"], projected["targets"])
    # the pinned contract hash wins over a recomputation when both exist
    incoming_hash = str(projected["content"].get("content_hash") or incoming_hash)
    projected["content"]["content_hash"] = incoming_hash
    existing = store.get_by_content_id(content_id)
    sched = {k: body[k] for k in ("run_at", "schedule_kind", "cron_expr")
             if body.get(k)}

    if existing is None:
        pid = store.create(
            body=projected["body"], content=projected["content"],
            targets=projected["targets"], status=body.get("status") or "draft",
            schedule_kind=body.get("schedule_kind") or "now",
            run_at=body.get("run_at"), cron_expr=body.get("cron_expr"),
            campaign=projected["campaign"], expires_at=body.get("expires_at"),
        )
        if projected["policy"] == "required":
            store.pend_for_review(pid, decided_by="submit")
        return _ok(_envelope(store.get(pid), "created", errors, warnings), status=201)

    if (existing.get("content_hash") or "") == incoming_hash:
        if sched:
            store.update(existing["id"], **sched)  # schedule-only: never voids an approval
        return _ok(_envelope(store.get(existing["id"]), "replay", errors, warnings))

    if existing["status"] in _TERMINAL:
        return _err("content changed after publish — refusing (immutable)",
                    409, code="idempotency_conflict",
                    details={"stored_hash": existing.get("content_hash"),
                             "incoming_hash": incoming_hash})
    try:
        store.set_content(existing["id"], projected["content"], projected["targets"])
    except ValueError as exc:
        if str(exc).startswith("immutable"):
            return _err(str(exc), 409, code="immutable")
        return _err(str(exc), 404)
    if sched:
        store.update(existing["id"], **sched)
    # re-review: a required-policy post whose approval was invalidated/rejected/
    # expired re-enters the queue for a fresh decision (§4 step 7).
    if projected["policy"] == "required" and \
            store.get(existing["id"])["approval_state"] in ("invalidated", "rejected", "expired"):
        store.pend_for_review(existing["id"], decided_by="resubmit")
    return _ok(_envelope(store.get(existing["id"]), "updated", errors, warnings))


async def handle_studio_post_get(request: "web.Request") -> "web.Response":
    try:
        pid = int(request.match_info["id"])
    except (KeyError, ValueError):
        return _err("invalid id", status=400)
    post = _store().get(pid)
    return _ok({"post": post}) if post else _err("post not found", status=404)


async def handle_studio_post_update(request: "web.Request") -> "web.Response":
    try:
        pid = int(request.match_info["id"])
    except (KeyError, ValueError):
        return _err("invalid id", status=400)
    post = _store().get(pid)
    if not post:
        return _err("post not found", status=404)
    body = await _body(request)
    content_keys = {"body", "content", "targets"} & set(body)
    if content_keys:
        # terminal first: the correct answer is 409 immutable, never 404/400
        if post["status"] in _TERMINAL:
            return _err(f"post is {post['status']} — content is immutable", 409,
                        code="immutable", details={"status": post["status"]})
        if post.get("content_id"):
            # identity is the contract: an external post's content edit must carry
            # it (content.content_hash / content.contract), else the hash cannot
            # be rebound honestly — silently keeping the old one would let an
            # edit sail under an existing approval.
            sent = body.get("content") if isinstance(body.get("content"), dict) else None
            if sent is None or not (sent.get("contract") or sent.get("content_hash")):
                return _err("external posts require content with contract/content_hash when editing",
                            422, code="contract_required",
                            details={"hint": "resubmit via POST /studio/posts "
                                             "with the external block"})
    if "content" in body or "targets" in body:
        # material edit → rehash + version bump + approval invalidation (G1/G2).
        # Rescheduling (run_at below) never comes through here — schedule is
        # excluded from the hash, so an approved post may be moved in time.
        try:
            _store().set_content(
                pid,
                body["content"] if "content" in body else post["content"],
                body["targets"] if "targets" in body else post["targets"],
            )
        except ValueError as exc:
            msg = str(exc)
            if msg.startswith("immutable"):
                return _err(msg, 409, code="immutable")
            return _err(msg, status=404)
        rest = {k: body[k] for k in
                ("body", "status", "schedule_kind", "run_at", "cron_expr",
                 "expires_at", "campaign") if k in body}
        if rest and _store().update(pid, **rest):
            pass
        return _ok({"post": _store().get(pid)})
    fields: dict[str, Any] = {}
    for k in ("body", "status", "schedule_kind", "run_at", "cron_expr",
              "expires_at", "campaign"):
        if k in body:
            fields[k] = body[k]
    if not _store().update(pid, **fields):
        # a refused content edit on a terminal post is 409, not "nothing updated"
        if content_keys and post["status"] in _TERMINAL:
            return _err(f"post is {post['status']} — content is immutable", 409,
                        code="immutable", details={"status": post["status"]})
        return _err("nothing updated", status=400)
    return _ok({"post": _store().get(pid)})


async def handle_studio_post_approve(request: "web.Request") -> "web.Response":
    """Bind an approval to the post's EXACT current content hash (G1)."""
    try:
        pid = int(request.match_info["id"])
    except (KeyError, ValueError):
        return _err("invalid id", status=400)
    body = await _body(request)
    try:
        post = _store().approve(pid, decided_by=str(body.get("by", "")),
                                content_hash=body.get("content_hash") or None)
    except ValueError as exc:
        stale = "stale approval" in str(exc)
        missing = str(exc).startswith("no post")
        return _err(str(exc), status=409 if stale else (404 if missing else 400))
    return _ok({"post": post})


async def handle_studio_post_reject(request: "web.Request") -> "web.Response":
    """Record an operator rejection (G1)."""
    try:
        pid = int(request.match_info["id"])
    except (KeyError, ValueError):
        return _err("invalid id", status=400)
    body = await _body(request)
    try:
        post = _store().reject(pid, reason=str(body.get("reason", "")),
                               decided_by=str(body.get("by", "")))
    except ValueError as exc:
        return _err(str(exc), status=404)
    return _ok({"post": post})


async def handle_studio_post_delete(request: "web.Request") -> "web.Response":
    try:
        pid = int(request.match_info["id"])
    except (KeyError, ValueError):
        return _err("invalid id", status=400)
    return _ok({"deleted": _store().delete(pid)})


async def handle_studio_post_publish(request: "web.Request") -> "web.Response":
    try:
        pid = int(request.match_info["id"])
    except (KeyError, ValueError):
        return _err("invalid id", status=400)
    post = _store().get(pid)
    if not post:
        return _err("post not found", status=404)
    try:
        body = await _body(request)
        headers = getattr(request, "headers", None) or {}
        key = (headers.get("Idempotency-Key") or headers.get("idempotency-key")
               or body.get("idempotency_key"))
        # G5: a replayed key on an already-published post returns the stored
        # result — a double POST can never publish twice.
        if (key and post.get("idempotency_key") == key
                and post.get("status") in ("published", "partial")):
            return _ok({"id": post["id"], "status": "replayed",
                        "receipts": post.get("receipts") or [], "replayed": True})
        # Expiry: an expired item never publishes (410, not a silent skip).
        expires = post.get("expires_at")
        if expires and expires <= _now_iso():
            return _err("post expired before publish", 410, code="expired",
                        details={"expires_at": expires})
        # Explicit-publish gate: the scheduler answers 'awaiting_approval'
        # (stays parked), but a synchronous POST deserves the honest refusal —
        # 409 with the hash the approval must match. Same authority as
        # approval_ok(): state is the view, the hash is the check.
        store = _store()
        if not store.approval_ok(post):
            return _err("awaiting approval", 409, code="approval_required",
                        details={"approval_state": post.get("approval_state") or "none",
                                 "approved_hash": post.get("approved_hash"),
                                 "current_hash": post.get("content_hash")})
        from navig_social.social.scheduler_service import ScheduledPostService

        gateway = request.app.get("gateway") if hasattr(request, "app") else None
        svc = ScheduledPostService(gateway)
        result = await svc.run_post(store, post, idempotency_key=key)
        return _ok(result)
    except Exception as exc:
        logger.exception("studio publish failed")
        return _err(str(exc), status=502)


async def handle_studio_receipts(request: "web.Request") -> "web.Response":
    """The publish-receipt ledger, filterable (SOCIAL-CONTRACT §2 receipts)."""
    try:
        from navig_social.social.receipts import get_publish_receipts

        campaign = request.query.get("campaign") or None
        network = request.query.get("network") or None
        content_id = request.query.get("content_id") or None
        try:
            limit = max(1, min(1000, int(request.query.get("limit") or 200)))
        except ValueError:
            limit = 200
        rows = get_publish_receipts().list(campaign=campaign, content_id=content_id,
                                           limit=limit)
        if network:
            rows = [r for r in rows if r.get("network") == network]
        return _ok({"receipts": rows})
    except Exception as exc:
        logger.exception("studio receipts failed")
        return _err(str(exc))


# ─── AI assist ───────────────────────────────────────────────────────────────

_AI_PROMPTS = {
    "draft": "Write a concise, engaging social post about the following. Return only the post text.",
    "rewrite": "Rewrite this social post to be clearer and more engaging. Return only the rewritten text.",
    "shorten": "Shorten this social post while keeping its meaning. Return only the shortened text.",
    "hashtags": "Suggest 5-8 relevant hashtags for this post. Return them space-separated, each starting with #.",
    "variants": "Write 3 distinct variants of this social post, one per line. Return only the variants.",
}


async def handle_studio_ai(request: "web.Request") -> "web.Response":
    body = await _body(request)
    action = (body.get("action") or "rewrite").lower()
    text = (body.get("text") or "").strip()
    if not text:
        return _err("'text' is required", status=400)
    system = _AI_PROMPTS.get(action, _AI_PROMPTS["rewrite"])
    try:
        import asyncio

        # `navig.llm.generate` — NOT `navig.llm_generate`, which has never existed. The
        # ImportError was caught by the `except Exception` below, so this endpoint returned
        # 502 on every request instead of failing loudly. navig-email and navig-audio always
        # used the real path.
        from navig.llm.generate import llm_generate

        # Keywords, not position: the previous call passed `120` as the 7th positional
        # argument, which is `model_override` — the timeout is the 9th. Fixing only the
        # import would have swapped one 502 for a bogus model name.
        out = await asyncio.to_thread(
            llm_generate,
            [{"role": "system", "content": system}, {"role": "user", "content": text}],
            mode="summarize" if action in ("shorten", "hashtags") else "big_tasks",
            timeout=120,
        )
        return _ok({"action": action, "result": (out or "").strip()})
    except Exception as exc:
        logger.exception("studio ai failed")
        return _err(str(exc), status=502)


# ─── Registration ────────────────────────────────────────────────────────────


def register(app: "web.Application") -> None:
    # Every Studio endpoint is gated on the free "media" module being enabled —
    # disabling it in the module registry makes these return 403 module_disabled
    # (live, no restart). This is the runtime "remove" the extraction gives us.
    from navig.modules.gate import requires_module

    g = requires_module("social")
    app.router.add_get("/api/deck/studio/networks", g(handle_studio_networks))
    app.router.add_post("/api/deck/studio/media", g(handle_studio_media_upload))
    app.router.add_get("/api/deck/studio/media/{id}/raw", g(handle_studio_media_raw))
    app.router.add_get("/api/deck/studio/posts", g(handle_studio_posts_list))
    app.router.add_post("/api/deck/studio/posts", g(handle_studio_post_create))
    app.router.add_get("/api/deck/studio/posts/{id}", g(handle_studio_post_get))
    app.router.add_patch("/api/deck/studio/posts/{id}", g(handle_studio_post_update))
    app.router.add_delete("/api/deck/studio/posts/{id}", g(handle_studio_post_delete))
    app.router.add_post("/api/deck/studio/posts/{id}/publish", g(handle_studio_post_publish))
    app.router.add_post("/api/deck/studio/posts/{id}/approve", g(handle_studio_post_approve))
    app.router.add_post("/api/deck/studio/posts/{id}/reject", g(handle_studio_post_reject))
    app.router.add_get("/api/deck/social/receipts", g(handle_studio_receipts))
    app.router.add_post("/api/deck/studio/ai", g(handle_studio_ai))
