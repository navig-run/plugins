"""Deck/OS gateway API for navig-games — exposes the games engine over HTTP so the
deck UI, the OS super-app, remote access (lighthouse) and scripts can all consume
the same data the CLI does.

All routes live under ``/api/deck/games/*`` and are gated on the ``games`` module
being enabled (``requires_module`` → ``403 module_disabled`` when off, live, no
restart). Auth is handled by the gateway's global ``/api/deck/*`` middleware, so
handlers don't wrap it themselves. Blocking engine calls (network / disk) run in a
worker thread so the gateway event loop stays responsive.

    GET  /api/deck/games/status                  plugin + subsystem status
    GET  /api/deck/games/free                     current & upcoming free games (Epic)
    GET  /api/deck/games/deals?threshold=&fresh=  Steam wishlist deals (fresh=1 skips cache)
    GET  /api/deck/games/deals/watchlist          manual deals watchlist (with names)
    POST /api/deck/games/deals/watch              add/remove an appid on the watchlist
    GET  /api/deck/games/library                  installed games across launchers
    GET  /api/deck/games/history                  claim ledger (what's been claimed/attempted)
    POST /api/deck/games/unify                    add non-Steam games to Steam (+ art)
    POST /api/deck/games/claim                    claim this week's free games (background job)
    GET  /api/deck/games/claim/status             current/last claim-job status (UI poll target)
    POST /api/deck/games/login/capture            one-click Epic sign-in (capture / open login)
    POST /api/deck/games/schedule                 enable/disable an automation (claim|deals|unify)

Mirrors the navig-social deck-route pattern.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiohttp import web

_log = logging.getLogger(__name__)


def _ok(data, status: int = 200):
    from aiohttp import web

    return web.json_response({"ok": True, "data": data}, status=status)


def _err(msg: str, status: int = 500):
    from aiohttp import web

    return web.json_response({"ok": False, "error": msg}, status=status)


async def handle_status(request: "web.Request") -> "web.Response":
    try:
        data = await asyncio.to_thread(_status_payload)
        return _ok(data)
    except Exception as exc:  # noqa: BLE001 — routes must not 500 the gateway
        _log.warning("games route status: %s", exc)
        return _err(str(exc))


async def handle_free(request: "web.Request") -> "web.Response":
    try:
        from navig_games.engine import runner

        data = await asyncio.to_thread(runner.check)  # all stores (epic + steam)
        return _ok(data)
    except Exception as exc:  # noqa: BLE001
        _log.warning("games route free: %s", exc)
        return _err(str(exc))


async def handle_deals(request: "web.Request") -> "web.Response":
    try:
        from navig_games.engine.sources import steam

        try:
            threshold = int(request.query.get("threshold") or 20)
        except (TypeError, ValueError):
            threshold = 20
        fresh = str(request.query.get("fresh", "")).lower() in ("1", "true", "yes")
        r = await asyncio.to_thread(steam.check_deals, threshold=threshold, use_cache=not fresh)
        return _ok({**r, "deals": [d.to_dict() for d in r["deals"]]})
    except Exception as exc:  # noqa: BLE001
        _log.warning("games route deals: %s", exc)
        return _err(str(exc))


async def handle_watchlist(request: "web.Request") -> "web.Response":
    """GET — the manual deals watchlist, with resolved Steam names."""
    try:
        from navig_games.engine.sources import steam

        watches = await asyncio.to_thread(steam.watchlist_named)
        return _ok({"watches": watches, "count": len(watches)})
    except Exception as exc:  # noqa: BLE001
        _log.warning("games route watchlist: %s", exc)
        return _err(str(exc))


async def handle_watch(request: "web.Request") -> "web.Response":
    """POST — add/remove a Steam appid on the manual deals watchlist.

    Body: ``{appid: <int|store-url>, watch?: bool}`` — ``watch`` true adds (default),
    false removes. Owner-safe: loopback deck plane, gated on the ``games`` module.
    """
    try:
        try:
            body = await request.json() if request.can_read_body else {}
        except Exception:  # noqa: BLE001
            body = {}
        from navig_games.engine.sources import steam

        appid = steam.parse_appid(str(body.get("appid", "")))
        if not appid:
            return _err("give a Steam appid or a store URL (…/app/<id>/)", status=400)
        watch = bool(body.get("watch", True))
        fn = steam.add_watch if watch else steam.remove_watch
        changed = await asyncio.to_thread(fn, appid)
        return _ok({
            "appid": appid,
            "watching": watch,
            "changed": changed,
            "watchlist": steam.get_watchlist(),
        })
    except Exception as exc:  # noqa: BLE001
        _log.warning("games route watch: %s", exc)
        return _err(str(exc))


async def handle_grab(request: "web.Request") -> "web.Response":
    """POST — mark a free game as grabbed (or undo).

    Body: ``{game: <key>, grabbed?: bool}`` — the terminal state for everything we
    can't claim headlessly. Owner-safe: loopback deck plane, gated on the ``games``
    module. Delegates to ``runner.mark_grabbed`` (the one place the rule lives).
    """
    try:
        try:
            body = await request.json() if request.can_read_body else {}
        except Exception:  # noqa: BLE001
            body = {}
        key = str(body.get("game") or "").strip()
        if not key:
            return _err("give a game key (from /games/free)", status=400)
        grabbed = bool(body.get("grabbed", True))

        from navig_games.engine import runner

        r = await asyncio.to_thread(runner.mark_grabbed, key, grabbed=grabbed)
        if r.get("error"):
            return _err(r["error"], status=404 if "isn't free" in r["error"] else 409)
        return _ok(r)
    except Exception as exc:  # noqa: BLE001
        _log.warning("games route grab: %s", exc)
        return _err(str(exc))


async def handle_library(request: "web.Request") -> "web.Response":
    try:
        from navig_games.engine import library

        games = await asyncio.to_thread(library.scan)
        return _ok({"games": [g.to_dict() for g in games], "count": len(games)})
    except Exception as exc:  # noqa: BLE001
        _log.warning("games route library: %s", exc)
        return _err(str(exc))


async def handle_unify(request: "web.Request") -> "web.Response":
    """POST — add the user's non-Steam games to Steam (writes shortcuts.vdf).

    Body (all optional): ``{store?: "epic,gog,amazon", dry_run?: bool, force?: bool}``.
    The engine backs up shortcuts.vdf, dedupes, and refuses while Steam is running
    (surfaced here as 409). Owner-safe: reachable only via the loopback deck plane
    and gated on the ``games`` module.
    """
    try:
        try:
            body = await request.json() if request.can_read_body else {}
        except Exception:  # noqa: BLE001 — empty / non-JSON body → defaults
            body = {}
        store = body.get("store")
        stores = [s.strip().lower() for s in str(store).split(",") if s.strip()] if store else None
        dry_run = bool(body.get("dry_run", False))
        force = bool(body.get("force", False))

        from navig_games.engine import library
        from navig_games.engine.steam import grid, shortcuts

        def _run() -> dict:
            games = [g for g in library.scan_non_steam(stores) if g.launchable]
            if not games:
                return {"ok": True, "added": [], "skipped_present": [], "skipped_unlaunchable": [],
                        "message": "No non-Steam games with a resolvable executable were found."}
            # Best-effort cover art (keyless GOG-local) for games not already present.
            icons: dict[str, str] = {}
            if not dry_run:
                present = shortcuts.present_exes()
                for gm in games:
                    if gm.launch_exe.strip().lower() in present:
                        continue
                    grid.apply_for_game(gm)
                    ic = grid.icon_path_for(gm)
                    if ic:
                        icons[gm.key] = ic
            return shortcuts.add_games(games, dry_run=dry_run, force=force, icons=icons)

        result = await asyncio.to_thread(_run)
        if not result.get("ok"):
            # e.g. "Steam is running — quit Steam first" → 409 Conflict
            return _err(result.get("error", "unify failed"), status=409)
        return _ok(result)
    except Exception as exc:  # noqa: BLE001
        _log.warning("games route unify: %s", exc)
        return _err(str(exc))


async def handle_history(request: "web.Request") -> "web.Response":
    """GET — the claim ledger (what's been claimed / attempted), newest first."""
    try:

        def _load() -> list[dict]:
            from navig_games.engine.ledger import Ledger

            recs = Ledger().all()
            recs.sort(key=lambda r: r.get("updated") or "", reverse=True)
            return recs

        records = await asyncio.to_thread(_load)
        return _ok({"records": records, "count": len(records)})
    except Exception as exc:  # noqa: BLE001
        _log.warning("games route history: %s", exc)
        return _err(str(exc))


# --- claim job (single-slot, subprocess-backed) -------------------------------
# The claim drives a real browser for ~60s and uses ``cdp_runtime``'s process-global
# event loop — running it *inside* the daemon would fight the daemon's own browser
# loop. So we spawn ``navig games claim`` as a subprocess (the same isolation the
# scheduler uses), track a single in-flight job here, and let the UI poll status.
# Feedback also lands durably in the ledger (GET /history) + the notify fan-out.
_claim_lock: dict = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "dry_run": False,
    "game": None,  # a single game key when the per-game Claim button was used
    "ok": None,
    "summary": None,  # {claimed, results:[{title,status,message}], message, error}
}
_claim_task: "asyncio.Task | None" = None

# Hard ceiling on a background claim. `navig games claim` drives a headful browser; without
# this an unbounded communicate() would hang the task forever if the browser wedges — the
# finally would never clear _claim_lock["running"], permanently 409-ing every future claim.
_CLAIM_TIMEOUT_S = 900


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_from_output(out: bytes) -> "dict | None":
    """Extract the JSON object a ``navig … --json`` command prints.

    ``--json`` prints one *pretty-printed* object (the whole of stdout), so parse
    the entire payload; if stray log lines wrap it, fall back to the ``{`` … ``}``
    substring. Returns None when nothing parses to a dict.
    """
    import json as _json

    text = (out or b"").decode("utf-8", "replace").strip()
    if not text:
        return None
    candidates = [text]
    i, j = text.find("{"), text.rfind("}")
    if 0 <= i < j:
        candidates.append(text[i : j + 1])
    for cand in candidates:
        try:
            parsed = _json.loads(cand)
        except Exception:  # noqa: BLE001 — try the next candidate
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _parse_claim_output(out: bytes, err: bytes, returncode: int) -> dict:
    """Turn ``navig games claim --json`` output into a compact UI summary."""
    data = _json_from_output(out)
    if isinstance(data, dict):
        return {
            "claimed": data.get("claimed", 0),
            "login": data.get("login"),
            "results": [
                {"title": r.get("title"), "status": r.get("status"), "message": r.get("message")}
                for r in (data.get("results") or [])
            ],
            "message": data.get("message"),
            "error": data.get("error"),
        }
    errtext = (err or b"").decode("utf-8", "replace").strip()
    return {
        "claimed": 0,
        "results": [],
        "message": errtext[-200:] or "Claim finished (no result parsed).",
        "error": None if returncode == 0 else f"exit {returncode}",
    }


async def _run_claim(dry_run: bool, game: "str | None" = None) -> None:
    """Background task: spawn ``navig games claim`` and record the result. When
    ``game`` is set, claim only that game (the Free tab's per-game Claim button)."""
    global _claim_task
    args = ["navig", "games", "claim", "--json", "--dry-run" if dry_run else "--yes"]
    args += ["--game", game] if game else ["--all"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=_CLAIM_TIMEOUT_S)
        except asyncio.TimeoutError:
            # Wedged headful browser: kill the child so it isn't orphaned, then surface the
            # failure (the outer except records it, the finally clears _claim_lock["running"]).
            proc.kill()
            await proc.wait()
            raise TimeoutError(f"claim timed out after {_CLAIM_TIMEOUT_S}s and was killed")
        summary = _parse_claim_output(out, err, proc.returncode or 0)
        _claim_lock["ok"] = (proc.returncode == 0) and not summary.get("error")
        _claim_lock["summary"] = summary
    except Exception as exc:  # noqa: BLE001 — surface as a failed job, never crash the loop
        _log.warning("games claim job: %s", exc)
        _claim_lock["ok"] = False
        _claim_lock["summary"] = {"claimed": 0, "results": [], "message": str(exc), "error": str(exc)}
    finally:
        _claim_lock["running"] = False
        _claim_lock["finished_at"] = _now_iso()
        _claim_task = None


async def handle_claim(request: "web.Request") -> "web.Response":
    """POST — claim this week's free games in the background (FREE-ONLY).

    Body (optional): ``{dry_run?: bool, game?: str}`` — ``game`` (a ledger key) claims
    just that one freebie; otherwise all current freebies. One claim runs at a time —
    a second request while one is in flight → 409. Returns immediately; poll
    ``/claim/status`` for progress, or read ``/history``. Owner-safe: reachable only
    via the loopback deck plane and gated on ``games``.
    """
    global _claim_task
    try:
        try:
            body = await request.json() if request.can_read_body else {}
        except Exception:  # noqa: BLE001 — empty / non-JSON body → defaults
            body = {}
        dry_run = bool(body.get("dry_run", False))
        game = body.get("game")
        game = str(game) if game else None
        if _claim_lock["running"]:
            return _err("A claim is already running — check the status.", status=409)
        _claim_lock.update(
            {
                "running": True,
                "started_at": _now_iso(),
                "finished_at": None,
                "dry_run": dry_run,
                "game": game,
                "ok": None,
                "summary": None,
            }
        )
        _claim_task = asyncio.create_task(_run_claim(dry_run, game))
        return _ok({"running": True, "started_at": _claim_lock["started_at"], "dry_run": dry_run, "game": game})
    except Exception as exc:  # noqa: BLE001
        _log.warning("games route claim: %s", exc)
        return _err(str(exc))


async def handle_claim_status(request: "web.Request") -> "web.Response":
    """GET — current/last claim-job status (the UI poll target)."""
    try:
        keys = ("running", "started_at", "finished_at", "dry_run", "game", "ok", "summary")
        return _ok({k: _claim_lock.get(k) for k in keys})
    except Exception as exc:  # noqa: BLE001
        _log.warning("games route claim status: %s", exc)
        return _err(str(exc))


def _apply_schedule_live(name: str, command: str, when: str, enabled: bool, job: str, sched) -> bool:
    """Enable/disable a games cron job. Prefers the LIVE ``CronService`` (in-daemon,
    so it takes effect immediately) and falls back to the jobs file schedule.py
    writes (loaded at the next daemon start). The live service persists to the same
    file schedule.py reads (compatible shape), so ``status`` stays consistent.

    Runs inline on the event-loop thread on purpose: the scheduler loop lives on the
    same loop, so mutating its jobs here can't race a worker thread.
    """
    try:
        from navig.scheduler.cron_service import get_live_service

        svc = get_live_service()
    except Exception:  # noqa: BLE001
        svc = None
    if svc is not None:
        try:
            existing = next((j for j in svc.list_jobs() if j.name == name), None)
            if enabled and existing is None:
                svc.add_job(name=name, schedule=when, command=command)
            elif enabled and existing is not None:
                svc.enable_job(existing.id)
            elif not enabled and existing is not None:
                svc.remove_job(existing.id)
            return True
        except Exception as exc:  # noqa: BLE001 — fall back to the file path
            _log.warning("games schedule live apply failed (%s); using file fallback", exc)
    file_fns = {
        "claim": (sched.enable, sched.disable),
        "deals": (sched.enable_deals, sched.disable_deals),
        "unify": (sched.enable_unify, sched.disable_unify),
    }[job]
    file_fns[0](when) if enabled else file_fns[1]()
    return False


async def handle_schedule(request: "web.Request") -> "web.Response":
    """POST — enable/disable a games automation (the CLI's ``navig games schedule``
    over HTTP). Body: ``{job: "claim"|"deals"|"unify", enabled: bool}``. Owner-safe:
    loopback deck plane, gated on the ``games`` module. Returns the new state.
    """
    try:
        try:
            body = await request.json() if request.can_read_body else {}
        except Exception:  # noqa: BLE001
            body = {}
        job = str(body.get("job") or "").lower()
        enabled = bool(body.get("enabled", False))

        from navig_games.engine import schedule as sched

        jobs = {
            "claim": (sched.CLAIM_JOB, sched.CLAIM_COMMAND, "daily", sched.status),
            "deals": (sched.DEALS_JOB, sched.DEALS_COMMAND, "daily", sched.deals_status),
            "unify": (sched.UNIFY_JOB, sched.UNIFY_COMMAND, "weekly", sched.unify_status),
        }
        if job not in jobs:
            return _err(f"unknown job '{job}' — expected claim, deals or unify", status=400)
        name, command, when, status_fn = jobs[job]

        live = _apply_schedule_live(name, command, when, enabled, job, sched)
        st = await asyncio.to_thread(status_fn)
        return _ok({
            "job": job,
            "enabled": bool(st.get("enabled")),
            "schedule": st.get("schedule"),
            "live": live,
        })
    except Exception as exc:  # noqa: BLE001
        _log.warning("games route schedule: %s", exc)
        return _err(str(exc))


async def handle_login_capture(request: "web.Request") -> "web.Response":
    """POST — one-click Epic sign-in for the OS app.

    Runs ``navig games login epic --capture-only`` as a subprocess (browser work
    off the daemon loop): if the persistent profile is already signed in it saves
    the session to the vault (``{ok:true, name}``); otherwise it opens the Epic
    login page (``{ok:false, opened:true}``) so the user can sign in and retry.
    409 while a claim is running (they share the one Epic browser profile).
    """
    try:
        if _claim_lock["running"]:
            return _err("A claim is running — try signing in again in a moment.", status=409)

        async def _run() -> dict:
            proc = await asyncio.create_subprocess_exec(
                "navig", "games", "login", "epic", "--capture-only", "--json",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=150)
            except asyncio.TimeoutError:
                # wait_for cancels only the communicate() coroutine — the headful login
                # browser keeps running orphaned. Kill the child before the timeout surfaces.
                proc.kill()
                await proc.wait()
                raise
            data = _json_from_output(out) or {}
            if not data:
                errtext = (err or b"").decode("utf-8", "replace").strip()
                return {"ok": False, "error": errtext[-200:] or "sign-in failed"}
            return {
                "ok": bool(data.get("ok")),
                "name": data.get("name"),
                "opened": bool(data.get("opened")),
                "error": data.get("error"),
            }

        return _ok(await _run())
    except asyncio.TimeoutError:
        return _err("Sign-in timed out — finish in the browser, then try again.", status=504)
    except Exception as exc:  # noqa: BLE001
        _log.warning("games route login capture: %s", exc)
        return _err(str(exc))


def _status_payload() -> dict:
    from navig_games import __version__
    from navig_games.engine import last_run, library, schedule, settings
    from navig_games.engine.library.steam import steam_path

    games = library.scan()
    by: dict[str, int] = {}
    for g in games:
        by[g.store] = by.get(g.store, 0) + 1

    epic_signed_in = False
    epic_account = None
    epic_session_expired = False
    try:
        from navig_games.engine.claim.epic import (
            epic_session_expired as _expired,
        )
        from navig_games.engine.claim.epic import (
            epic_session_present,
        )

        epic_signed_in, epic_account = epic_session_present()
        epic_session_expired = _expired()
    except Exception:  # noqa: BLE001
        pass

    return {
        "version": __version__,
        "epic_account": epic_account,
        "region": {"country": settings.country(), "locale": settings.locale()},
        "epic_signed_in": epic_signed_in,
        # A vaulted session can be *present* (epic_signed_in) yet *dead*: this is
        # true when the last auto-claim couldn't sign in AND it hasn't been fixed
        # since (a re-login clears it), so the tile can prompt a re-login instead of
        # a stale green light. Free — reads last_run + vault meta, no browser probe
        # (the live check stays on-demand: doctor --live).
        "epic_session_expired": epic_session_expired,
        "installed": {"count": len(games), "by_store": by},
        "steam_detected": steam_path() is not None,
        "steamgriddb_key": bool(settings.get("steamgriddb_key")),
        "schedules": {
            "claim": schedule.status().get("enabled", False),
            "deals": schedule.deals_status().get("enabled", False),
            "unify": schedule.unify_status().get("enabled", False),
        },
        "last_run": last_run.read(),
    }


def register(app: "web.Application") -> None:
    """Mount the games API (each route gated on the ``games`` module being enabled)."""
    from navig.modules.gate import requires_module

    g = requires_module("games")
    app.router.add_get("/api/deck/games/status", g(handle_status))
    app.router.add_get("/api/deck/games/free", g(handle_free))
    app.router.add_get("/api/deck/games/deals", g(handle_deals))
    app.router.add_get("/api/deck/games/deals/watchlist", g(handle_watchlist))
    app.router.add_post("/api/deck/games/deals/watch", g(handle_watch))
    app.router.add_post("/api/deck/games/grab", g(handle_grab))
    app.router.add_get("/api/deck/games/library", g(handle_library))
    app.router.add_get("/api/deck/games/history", g(handle_history))
    app.router.add_post("/api/deck/games/unify", g(handle_unify))
    app.router.add_post("/api/deck/games/claim", g(handle_claim))
    app.router.add_get("/api/deck/games/claim/status", g(handle_claim_status))
    app.router.add_post("/api/deck/games/login/capture", g(handle_login_capture))
    app.router.add_post("/api/deck/games/schedule", g(handle_schedule))
    _log.debug("navig-games: deck API routes mounted (/api/deck/games/*)")
