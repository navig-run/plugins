"""Click-tracking redirect — the last mile that makes the measure loop automatic.

A published tracking link ``<base>/r/<campaign>/<network>`` (see
``navig_social.social.fanout.tracking_link``) resolves to the real destination
**from our own receipt ledger** — never a query parameter — records a click in
the engagement ledger, then 302s to the destination.

Resolving from the ledger (not a `?u=` param) is deliberate: it makes the endpoint
an **open-redirect-free** tracker — it can only send a visitor to a URL this
operator already published for that campaign+network. Publicly reachable because
lighthouse forwards arbitrary paths to the gateway; no lighthouse change needed.

    GET /r/{campaign}/{network}   → record click, 302 to the published URL (404 if none)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiohttp import web

_log = logging.getLogger(__name__)


async def handle_click_redirect(request: "web.Request") -> "web.Response":
    from aiohttp import web

    campaign = request.match_info.get("campaign", "")
    network = request.match_info.get("network", "")

    from navig_social.social.receipts import get_publish_receipts

    target = get_publish_receipts().latest_url(campaign, network) if campaign and network else None
    if not target:
        return web.Response(status=404, text="unknown campaign/network")

    # Record the click — best-effort; a storage hiccup must never break the redirect.
    try:
        from navig_social.social.engagement import get_engagement

        get_engagement().record(
            campaign=campaign, network=network, metric="clicks", value=1, source="redirect"
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("click record skipped: %s", exc)

    return web.HTTPFound(location=target)  # 302 → the real destination


def register(app: "web.Application") -> None:
    """Mount the click redirect (gated on the ``social`` module)."""
    from navig.modules.gate import requires_module

    g = requires_module("social")
    app.router.add_get("/r/{campaign}/{network}", g(handle_click_redirect))
