"""Campaign scorecard — join the receipt + engagement ledgers into a performance view.

The capstone of create → publish → measure: `navig social report` answers "how did
this campaign do?" by joining *what we published* (the receipt ledger — networks,
links, ok/fail) with *how it performed* (the engagement ledger — clicks / views per
network), and derives a CTR where views exist.

Pure data assembly (no rendering, no I/O beyond the two stores) so it's trivially
testable; the CLI in ``commands/social.py`` renders the returned structures.
"""

from __future__ import annotations

from typing import Any


def _ctr(clicks: int, views: int) -> float | None:
    """clicks ÷ views, or None when there are no views to divide by."""
    return (clicks / views) if views else None


def campaign_scorecard(campaign: str) -> dict[str, Any]:
    """A per-network scorecard for one campaign: publish status + link + clicks/views/CTR."""
    from navig_social.social.engagement import get_engagement
    from navig_social.social.receipts import get_publish_receipts

    receipts = get_publish_receipts().list(campaign=campaign, limit=1000)  # newest-first
    net_metrics = get_engagement().network_metrics(campaign)

    networks: dict[str, dict[str, Any]] = {}

    def _slot(net: str) -> dict[str, Any]:
        return networks.setdefault(
            net, {"network": net, "posts": 0, "ok": 0, "link": None, "clicks": 0, "views": 0, "last": ""}
        )

    for r in receipts:
        e = _slot(r["network"])
        e["posts"] += 1
        if r["ok"]:
            e["ok"] += 1
        if e["link"] is None and r["url"]:  # list is newest-first → first seen = latest link
            e["link"] = r["url"]
        if not e["last"]:
            e["last"] = r["created_at"]
    for net, metrics in net_metrics.items():
        e = _slot(net)  # a network can have clicks even if the publish wasn't recorded here
        e["clicks"] = metrics.get("clicks", 0)
        e["views"] = metrics.get("views", 0)

    rows = sorted(networks.values(), key=lambda e: (-e["clicks"], e["network"]))
    for e in rows:
        e["ctr"] = _ctr(e["clicks"], e["views"])
    total_clicks = sum(e["clicks"] for e in rows)
    total_views = sum(e["views"] for e in rows)
    return {
        "campaign": campaign,
        "networks": rows,
        "totals": {
            "posts": sum(e["posts"] for e in rows),
            "ok": sum(e["ok"] for e in rows),
            "clicks": total_clicks,
            "views": total_views,
            "ctr": _ctr(total_clicks, total_views),
        },
    }


def leaderboard(*, limit: int = 50) -> list[dict[str, Any]]:
    """All campaigns ranked by clicks: posts + clicks + views + CTR + last activity."""
    from navig_social.social.engagement import get_engagement
    from navig_social.social.receipts import get_publish_receipts

    camps = get_publish_receipts().campaigns(limit=limit)
    roll = get_engagement().rollup()
    out: list[dict[str, Any]] = []
    for c in camps:
        m = roll.get(c["campaign"] or "", {})
        clicks, views = m.get("clicks", 0), m.get("views", 0)
        out.append({
            "campaign": c["campaign"], "posts": c["count"], "ok": c["ok"],
            "clicks": clicks, "views": views, "ctr": _ctr(clicks, views), "last": c["last"],
        })
    out.sort(key=lambda e: (-e["clicks"], e["campaign"] or ""))
    return out
