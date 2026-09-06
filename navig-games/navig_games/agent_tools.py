"""Agent tools — let the NAVIG agent find & claim free games.

``games_check`` is read-only (safe). ``games_claim`` touches the user's real
store account, so it is ``owner_only`` and hidden until an Epic session is saved.
Browser work is offloaded with ``asyncio.to_thread`` onto core's dedicated CDP
loop (via the sync runner) to avoid nesting event loops inside the agent's loop.
"""

from __future__ import annotations

import asyncio
import logging

from navig.tools.registry import BaseTool, ToolResult

_log = logging.getLogger(__name__)

EPIC_DOMAIN = "epicgames.com"


class GamesCheckTool(BaseTool):
    name = "games_check"
    description = (
        "List the video games that are FREE right now (and upcoming) on the Epic "
        "Games Store. Read-only; does not sign in or claim anything."
    )
    owner_only = False
    parameters = [
        {"name": "upcoming", "type": "boolean", "required": False,
         "description": "Include upcoming freebies as well as current ones."},
    ]

    async def run(self, args, on_status=None) -> ToolResult:
        try:
            from navig_games.engine import runner

            data = await asyncio.to_thread(runner.check, "epic")
            current = data.get("current", [])
            out = {
                "free_now": [{"title": g["title"], "was": g.get("original_price"),
                              "ends": g.get("ends_at"), "url": g.get("url")} for g in current],
            }
            if args.get("upcoming"):
                out["upcoming"] = [{"title": g["title"], "starts": g.get("starts_at")}
                                   for g in data.get("upcoming", [])]
            return ToolResult(self.name, True, output=out)
        except Exception as exc:  # noqa: BLE001 — tools must never raise
            return ToolResult(self.name, False, error=str(exc))


class GamesDealsTool(BaseTool):
    name = "games_deals"
    description = (
        "List current price drops and free-to-keep giveaways on the user's Steam "
        "wishlist (+ manual watchlist). Read-only; public Steam data, no login."
    )
    owner_only = False
    parameters = [
        {"name": "threshold", "type": "integer", "required": False,
         "description": "Minimum discount percent to include (default 20)."},
    ]

    async def run(self, args, on_status=None) -> ToolResult:
        try:
            from navig_games.engine.sources import steam

            threshold = int(args.get("threshold", 20) or 20)
            r = await asyncio.to_thread(steam.check_deals, threshold=threshold)
            out = [{"name": d.name, "discount_pct": d.discount_pct, "price": d.final_formatted,
                    "free_to_keep": d.is_free, "on_wishlist": d.on_wishlist, "url": d.url}
                   for d in r["deals"]]
            return ToolResult(self.name, True, output={"deals": out, "checked": r["checked"]})
        except Exception as exc:  # noqa: BLE001
            return ToolResult(self.name, False, error=str(exc))


class GamesClaimTool(BaseTool):
    name = "games_claim"
    description = (
        "Claim the currently-FREE Epic Games Store games into the user's account. "
        "FREE-ONLY: only a $0 checkout is ever completed — a priced title is always "
        "refused, so this never spends money. Use dry_run to verify without ordering."
    )
    owner_only = True
    #: Completes a real checkout against the operator's store account using their
    #: vaulted session. Free-only, so it cannot spend money — but it is still a write
    #: to an external account performed on their behalf, so it asks first.
    #: `owner_only` is an AUTHORIZATION flag (~20 read-only devops tools set it) and is
    #: deliberately not what the approval gate reads; `safety` is.
    safety = "dangerous"
    parameters = [
        {"name": "dry_run", "type": "boolean", "required": False,
         "description": "Verify the games are free and stop before placing the order."},
        {"name": "force", "type": "boolean", "required": False,
         "description": "Re-attempt every current freebie, even if already recorded as claimed."},
    ]

    async def run(self, args, on_status=None) -> ToolResult:
        try:
            from navig_games.engine import runner

            result = await asyncio.to_thread(
                runner.run_claim_sync,
                store="epic",
                dry_run=bool(args.get("dry_run", False)),
                force=bool(args.get("force", False)),
                do_notify=True,
            )
            if result.get("error"):
                return ToolResult(self.name, False, error=result["error"])
            summary = {
                "checked": result.get("checked"),
                "attempted": result.get("attempted"),
                "claimed": result.get("claimed"),
                "dry_run": result.get("dry_run"),
                "results": [{"title": r["title"], "status": r["status"], "note": r.get("message")}
                            for r in result.get("results", [])],
            }
            return ToolResult(self.name, True, output=summary)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(self.name, False, error=str(exc))


class GamesLibraryTool(BaseTool):
    name = "games_library"
    description = (
        "List the games installed on this machine across Steam, Epic, GOG and "
        "Amazon (local scan, no login). Read-only."
    )
    owner_only = False
    parameters = [
        {"name": "stores", "type": "string", "required": False,
         "description": "Comma-separated subset, e.g. 'gog,epic'. Omit for all."},
    ]

    async def run(self, args, on_status=None) -> ToolResult:
        try:
            from navig_games.engine import library

            stores = None
            raw = args.get("stores")
            if raw:
                stores = [s.strip().lower() for s in str(raw).split(",") if s.strip()]
            games = await asyncio.to_thread(library.scan, stores)
            out = [{"store": g.store, "title": g.title, "launchable": g.launchable,
                    "exe": g.launch_exe} for g in games]
            return ToolResult(self.name, True, output={"installed": out, "count": len(out)})
        except Exception as exc:  # noqa: BLE001
            return ToolResult(self.name, False, error=str(exc))


def _has_epic_session() -> bool:
    """check_fn: only surface the claim tool once the user has signed in once."""
    try:
        from navig.vault.sessions import get_session

        return get_session(EPIC_DOMAIN) is not None
    except Exception:  # noqa: BLE001
        return False


def register_games_tools() -> None:
    """Register the games tools on the agent registry (toolset ``games``)."""
    try:
        from navig.agent.agent_tool_registry import _AGENT_REGISTRY as reg
    except Exception:  # noqa: BLE001 — agent subsystem absent
        return
    try:
        reg.register(GamesCheckTool(), toolset="games")
        reg.register(GamesLibraryTool(), toolset="games")
        reg.register(GamesDealsTool(), toolset="games")
        reg.register(GamesClaimTool(), toolset="games", check_fn=_has_epic_session)
    except Exception as exc:  # noqa: BLE001 — never block boot
        _log.warning("navig-games: agent tool registration skipped (%s)", exc)
