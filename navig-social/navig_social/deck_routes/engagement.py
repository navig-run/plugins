"""Engagement ingest — the HTTP surface for the measure half of the loop.

A UTM click beacon, a webhook, a platform poll, or the deck POSTs an engagement
event; a campaign rollup is available via GET. Mounted through the plugin's
``gateway:register_routes`` hook and gated on the ``social`` module — so it 404/403s
cleanly when navig-social is absent/disabled, just like the Studio routes.

    POST /api/deck/social/engagement   {campaign, metric, value?, network?, post_id?, source?}
    GET  /api/deck/social/engagement   [?campaign=<slug>]   → rollup / one campaign's metrics
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from navig_social.deck_routes.studio import _body, _err, _ok

if TYPE_CHECKING:
    from aiohttp import web


async def handle_engagement_ingest(request: "web.Request") -> "web.Response":
    body = await _body(request)
    campaign = (body.get("campaign") or "").strip()
    metric = (body.get("metric") or "").strip()
    if not campaign or not metric:
        return _err("campaign and metric are required", 400)
    try:
        value = int(body.get("value", 1))
    except (TypeError, ValueError):
        return _err("value must be an integer", 400)

    from navig_social.social.engagement import get_engagement

    rid = get_engagement().record(
        campaign=campaign,
        metric=metric,
        value=value,
        network=(body.get("network") or None),
        post_id=(body.get("post_id") or None),
        source=(body.get("source") or "ingest"),
    )
    return _ok({"id": rid, "campaign": campaign, "metric": metric, "value": value})


async def handle_engagement_rollup(request: "web.Request") -> "web.Response":
    from navig_social.social.engagement import get_engagement

    store = get_engagement()
    campaign = request.query.get("campaign")
    if campaign:
        return _ok({"campaign": campaign, "metrics": store.campaign_metrics(campaign)})
    return _ok({"campaigns": store.rollup()})


def register(app: "web.Application") -> None:
    """Mount the engagement ingest routes (gated on the ``social`` module)."""
    from navig.modules.gate import requires_module

    g = requires_module("social")
    app.router.add_post("/api/deck/social/engagement", g(handle_engagement_ingest))
    app.router.add_get("/api/deck/social/engagement", g(handle_engagement_rollup))
