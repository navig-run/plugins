"""Projects-hub link card: a project folder's Microsoft Store app and its recent sales.

A project is linked when it holds ``store/identity.json`` with a ``storeProductId``
(written by ``navig msstore configure``) — no guessing by name. The card reads the
local sales history only; it never calls Microsoft.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)
_HISTORY_TTL = 30.0
_history_cache: tuple[float, dict[str, Any]] | None = None


def _history() -> dict[str, Any]:
    global _history_cache
    now = time.monotonic()
    if _history_cache and now - _history_cache[0] < _HISTORY_TTL:
        return _history_cache[1]
    from navig_msstore.sales import load_history

    try:
        data = load_history()
    except Exception:  # noqa: BLE001 — a locked history file just means no numbers this time
        data = {"rows": {}}
    _history_cache = (now, data)
    return data


def store_card(project_path: str, space_id: str) -> dict[str, Any] | None:
    ident_path = Path(project_path) / "store" / "identity.json"
    if not ident_path.is_file():
        return None
    try:
        ident = json.loads(ident_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = str(ident.get("storeProductId") or "").strip()
    if not pid:
        return None
    rows = [r for r in (_history().get("rows") or {}).values() if r.get("app_id") == pid]
    since = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    recent = [r for r in rows if str(r.get("date") or "") >= since]
    units_30 = sum(int(r.get("quantity") or 0) for r in recent)
    usd_30 = round(sum(float(r.get("usd") or 0) for r in recent), 2)
    usd_all = round(sum(float(r.get("usd") or 0) for r in rows), 2)
    name = next((r.get("app_name") for r in rows if r.get("app_name")), None) or ident.get("displayName") or pid
    return {
        "title": f"Microsoft Store · {name}",
        "summary": (f"{units_30} sale(s) · ${usd_30:,.2f} in 30 days" if rows
                    else "Linked — no sales recorded yet"),
        "metrics": [
            {"id": "sales_30d", "label": "Sales (30d)", "value": units_30},
            {"id": "revenue_30d", "label": "Revenue (30d)", "value": usd_30, "unit": "USD"},
            {"id": "revenue_total", "label": "Revenue (tracked)", "value": usd_all, "unit": "USD"},
        ],
        "store_product_id": pid,
        "href": "apps/sales",
        "match": "store",
    }
