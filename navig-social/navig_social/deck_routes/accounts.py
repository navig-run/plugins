"""Connect publishing networks from the app — and list them in core's services catalog.

``navig social connect <network>`` was the only way in: it opens a browser and, for
paste-back providers, prompts on the terminal. The app's services list now runs the same
flows through the daemon (``oauth.run_headless``): the flow hands its authorize URL back to
the app instead of opening a browser, and a paste-back provider waits for the URL the app
posts. Nothing about the flows themselves changes.

Routes (all under /api/deck, gated on the ``social`` module):
  POST   /studio/networks/{network}/connect        start → {job, auth_url, paste}
  POST   /studio/networks/{network}/connect/reply  {job, reply} for paste-back providers
  GET    /studio/networks/connect/{job}            {state, account, error}
  POST   /studio/networks/{network}/credentials    {values: {path: value}} app creds / token
  DELETE /studio/networks/{network}[?account=id]   forget one account (keep app creds)

``service_rows()`` is registered with core's ``service_sources`` as "social".
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from urllib.parse import quote
from dataclasses import dataclass, field
from typing import Any

try:
    from aiohttp import web
except ImportError:
    web = None

logger = logging.getLogger(__name__)

_BASE = "/api/deck/studio/networks"
_JOB_TTL = 15 * 60
_ERROR_TTL = 10 * 60
_MAX_VALUE_LEN = 8 * 1024

_DESCRIPTIONS = {
    "facebook": "Publish to a Facebook Page you manage.",
    "instagram": "Publish to an Instagram professional account.",
    "threads": "Post to Threads.",
    "linkedin": "Post to your LinkedIn profile.",
    "youtube": "Upload videos to your YouTube channel.",
    "pinterest": "Create pins on your boards.",
    "devto": "Publish articles on dev.to.",
    "twitter": "Post to X.",
    "reddit": "Submit posts to a subreddit.",
}
_NAMES = {"youtube": "YouTube channel", "facebook": "Facebook Page", "twitter": "X (Twitter)"}
_ICONS = {
    "facebook": "📘", "instagram": "📸", "threads": "🧵", "linkedin": "💼", "youtube": "📺",
    "pinterest": "📌", "devto": "👩‍💻", "twitter": "𝕏", "reddit": "👽",
}
# Providers whose stored secret is a key the operator pastes (no OAuth flow).
_TOKEN_ONLY = {"devto": "dev.to API key", "twitter": "X access token", "reddit": "Reddit access token"}


@dataclass
class _Job:
    id: str
    network: str
    flow: Any
    created: float = field(default_factory=time.time)
    state: str = "running"  # running | done | error
    account: str = ""
    error: str = ""


_JOBS: dict[str, _Job] = {}
_LAST_ERROR: dict[str, tuple[str, float]] = {}
_LOCK = threading.Lock()


def _prune() -> None:
    now = time.time()
    with _LOCK:
        for jid in [j for j, job in _JOBS.items() if now - job.created > _JOB_TTL]:
            _JOBS.pop(jid, None)


def _running_job() -> _Job | None:
    with _LOCK:
        return next((j for j in _JOBS.values() if j.state == "running"), None)


def _field_label(path: str) -> str:
    from navig_social.social.labels import display_label

    last = path.rsplit("/", 1)[-1]
    human = {"app_id": "App ID", "app_secret": "App secret", "client_id": "Client ID",
             "client_secret": "Client secret"}.get(last, last.replace("_", " ").capitalize())
    # Instagram signs in with the Facebook app, so name the app the value belongs to.
    return f"{display_label(path.split('/', 1)[0])} {human}"


def _fields(paths: tuple[str, ...]) -> list[dict[str, Any]]:
    return [
        {
            "name": p,
            "label": _field_label(p),
            "secret": p.endswith("secret"),
            "multiline": False,
            "optional": False,
            "placeholder": "",
        }
        for p in paths
    ]


def _networks() -> list[str]:
    from navig_social.social import oauth

    names = list(oauth.CONNECTORS)
    try:
        from navig_social.social.registry import get_publisher_registry

        for pub in get_publisher_registry().all():
            if pub.name in _TOKEN_ONLY and pub.name not in names:
                names.append(pub.name)
    except Exception as exc:  # noqa: BLE001 — the OAuth providers still list
        logger.debug("publisher registry unavailable: %s", exc)
    return names


def service_rows() -> list[dict[str, Any]]:
    """Rows for core's ``GET /api/deck/services`` (one per publishing network)."""
    from navig_social.social import oauth
    from navig_social.social.accounts import list_accounts, refresh_label
    from navig_social.social.labels import display_label

    now = time.time()
    rows = []
    for name in _networks():
        token = oauth.read_secret(name)
        linked = list_accounts(name) if token else []
        connect: dict[str, Any] = {
            "remove": f"{_BASE}/{name}",
            "save": f"{_BASE}/{name}/credentials",
        }
        if name in _TOKEN_ONLY:
            connect.update(kind="api_key", ready=True, fields=[{
                "name": name, "label": _TOKEN_ONLY[name], "secret": True,
                "multiline": False, "optional": False, "placeholder": "",
            }])
        else:
            paths = oauth.app_credential_paths(name)
            missing = [p for p in paths if not oauth.read_secret(p)]
            connect.update(
                kind="oauth",
                ready=not missing,
                start=f"{_BASE}/{name}/connect",
                paste=name in oauth.MANUAL_ONLY,
                reply=f"{_BASE}/{name}/connect/reply",
                redirect_uri=oauth.REDIRECT_URI if name not in oauth.MANUAL_ONLY else "",
            )
            if missing:
                connect["fields"] = _fields(paths)
                connect["reason"] = f"Needs your {display_label(paths[0].split('/', 1)[0])} app credentials first."
        err = _LAST_ERROR.get(name)
        if err and now - err[1] < _ERROR_TTL and not token:
            connect["last_error"] = err[0]
        status = "connected" if token else ("available" if connect.get("ready") else "needs_setup")
        rows.append({
            "id": f"social:{name}",
            "name": _NAMES.get(name) or display_label(name),
            "description": _DESCRIPTIONS.get(name, ""),
            "icon": _ICONS.get(name, "🔗"),
            "category": "social",
            "source": "social",
            "status": status,
            "accounts": [
                {
                    "id": a["id"] or "default",
                    "label": a["label"] or a["id"] or "Connected",
                    "status": (
                        "needs_reconnect"
                        if name == "youtube"
                        and not oauth.read_secret(refresh_label(name, None if a["default"] else a["id"]))
                        else "ok"
                    ),
                    "remove": f"{_BASE}/{name}" + ("" if a["default"] else f"?account={quote(a['id'])}"),
                }
                for a in linked
            ],
            # Connecting another account adds it beside the first (social/accounts.py).
            "multi_account": name not in _TOKEN_ONLY,
            "connect": connect,
        })
    return rows


# ── Connect jobs ─────────────────────────────────────────────────────────────


def _run_job(job: _Job) -> None:
    from navig_social.social import oauth

    try:
        result = oauth.run_headless(
            job.flow, oauth.CONNECTORS[job.network], manual=job.network in oauth.MANUAL_ONLY
        )
        job.account = result.account  # save_account recorded it with the token
        job.state = "done"
        _LAST_ERROR.pop(job.network, None)
    except Exception as exc:  # noqa: BLE001 — every failure is reported to the app
        job.error = str(exc) or type(exc).__name__
        job.state = "error"
        _LAST_ERROR[job.network] = (job.error, time.time())
    finally:
        job.flow.url_ready.set()  # release a start() still waiting for a URL


def _job_body(job: _Job) -> dict[str, Any]:
    return {
        "job": job.id, "network": job.network, "state": job.state,
        "account": job.account, "error": job.error,
        "auth_url": job.flow.auth_url, "paste": job.flow.paste,
    }


async def handle_network_connect(request: "web.Request") -> "web.Response":
    from navig_social.social import oauth

    network = request.match_info.get("network", "")
    if network not in oauth.CONNECTORS:
        return web.json_response({"ok": False, "error": f"'{network}' has no sign-in flow"}, status=404)
    _prune()
    running = _running_job()
    if running is not None:
        if running.network == network and running.flow.auth_url:
            return web.json_response({"ok": True, **_job_body(running)})
        return web.json_response(
            {"ok": False, "error": "Another sign-in is still open — finish or wait for it first."},
            status=409,
        )
    job = _Job(id=uuid.uuid4().hex, network=network, flow=oauth.HeadlessFlow())
    with _LOCK:
        _JOBS[job.id] = job
    threading.Thread(target=_run_job, args=(job,), name=f"social-connect-{network}", daemon=True).start()
    await asyncio.to_thread(job.flow.url_ready.wait, 10.0)
    if job.state == "error":
        return web.json_response({"ok": False, **_job_body(job)}, status=422)
    if not job.flow.auth_url:
        return web.json_response({"ok": False, "error": "The sign-in did not start in time."}, status=504)
    return web.json_response({"ok": True, **_job_body(job)})


async def handle_network_connect_reply(request: "web.Request") -> "web.Response":
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
    job = _JOBS.get(str(body.get("job", "")))
    reply = body.get("reply")
    if job is None or job.network != request.match_info.get("network", ""):
        return web.json_response({"ok": False, "error": "unknown sign-in"}, status=404)
    if not isinstance(reply, str) or not reply.strip() or len(reply) > _MAX_VALUE_LEN:
        return web.json_response({"ok": False, "error": "paste the URL you were redirected to"}, status=400)
    if job.state != "running" or not job.flow.give_reply(reply.strip()):
        return web.json_response({"ok": False, "error": "this sign-in is no longer waiting"}, status=409)
    # Wait briefly for the exchange so the app can show the outcome right away.
    for _ in range(40):
        if job.state != "running":
            break
        await asyncio.sleep(0.25)
    return web.json_response({"ok": job.state != "error", **_job_body(job)})


async def handle_network_connect_status(request: "web.Request") -> "web.Response":
    job = _JOBS.get(request.match_info.get("job", ""))
    if job is None:
        return web.json_response({"ok": False, "error": "unknown sign-in"}, status=404)
    return web.json_response({"ok": True, **_job_body(job)})


# ── Credentials / disconnect ─────────────────────────────────────────────────


def _accepted_paths(network: str) -> set[str]:
    from navig_social.social import oauth

    if network in _TOKEN_ONLY:
        return {network}
    return set(oauth.app_credential_paths(network))


async def handle_network_credentials(request: "web.Request") -> "web.Response":
    from navig_social.social import oauth
    from navig_social.social.labels import display_label

    network = request.match_info.get("network", "")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
    values = body.get("values") if isinstance(body, dict) else None
    accepted = _accepted_paths(network)
    if not accepted:
        return web.json_response({"ok": False, "error": f"'{network}' takes no credentials"}, status=404)
    if not isinstance(values, dict) or not values:
        return web.json_response({"ok": False, "error": "values required"}, status=400)
    clean: dict[str, str] = {}
    for path, value in values.items():
        if path not in accepted:
            return web.json_response({"ok": False, "error": f"unknown field '{path}'"}, status=400)
        if not isinstance(value, str) or not value.strip() or len(value) > _MAX_VALUE_LEN:
            return web.json_response({"ok": False, "error": f"'{path}' is empty"}, status=400)
        clean[path] = value.strip()

    def _store() -> dict[str, Any]:
        if network in _TOKEN_ONLY:
            previous = oauth.read_secret(network)
            oauth.write_provider_token(network, clean[network], f"{display_label(network)} token")
            if network == "devto":
                # dev.to validates the key and reports the account. A rejected key must
                # not cost the operator the working one it was about to replace.
                try:
                    result = oauth.connect_devto()
                except oauth.SocialOAuthError:
                    if previous:
                        oauth.write_provider_token(network, previous, f"{display_label(network)} token")
                    else:
                        oauth.delete_secret(network)
                    raise
                oauth.set_config("devto", "account_label", result.account)
            return {"ok": True}
        for path, value in clean.items():
            oauth.write_path_secret(path, value)
        return {"ok": True}

    try:
        result = await asyncio.to_thread(_store)
    except oauth.SocialOAuthError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=422)
    except Exception as exc:  # noqa: BLE001 — a locked vault is the usual cause
        logger.error("saving %s credentials failed: %s", network, exc)
        return web.json_response({"ok": False, "error": f"Could not save to the vault: {exc}"}, status=500)
    return web.json_response(result)


async def handle_network_disconnect(request: "web.Request") -> "web.Response":
    from navig_social.social import oauth

    network = request.match_info.get("network", "")
    if network not in oauth.CONNECTORS and network not in _TOKEN_ONLY:
        return web.json_response({"ok": False, "error": f"unknown network '{network}'"}, status=404)
    account = (request.query.get("account") or "").strip() or None
    removed = await asyncio.to_thread(oauth.forget_provider, network, account)
    return web.json_response({"ok": True, "removed": removed})


def register(app: "web.Application") -> None:
    from navig.modules.gate import requires_module

    g = requires_module("social")
    # The literal path first, so "connect" is never captured as a {network}.
    app.router.add_get(f"{_BASE}/connect/{{job}}", g(handle_network_connect_status))
    app.router.add_post(f"{_BASE}/{{network}}/connect", g(handle_network_connect))
    app.router.add_post(f"{_BASE}/{{network}}/connect/reply", g(handle_network_connect_reply))
    app.router.add_post(f"{_BASE}/{{network}}/credentials", g(handle_network_credentials))
    app.router.add_delete(f"{_BASE}/{{network}}", g(handle_network_disconnect))

    try:
        from navig.gateway.deck.service_sources import register_service_source

        register_service_source("social", service_rows)
    except Exception as exc:  # noqa: BLE001 — an older core simply has no catalog
        logger.debug("services catalog unavailable: %s", exc)
