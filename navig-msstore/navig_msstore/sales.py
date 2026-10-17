"""Microsoft Store sales — what was bought, and what is NEW since the last look.

Partner Center's analytics API reports acquisitions as DAILY rows per app (and per
add-on for in-app purchases), so a "sale" is not an event Microsoft pushes: it is a
row whose quantity grew since we last read it. This module reads those rows, keeps
the paid ones (``purchasePriceUSDAmount > 0`` — free downloads are not sales), and
diffs them against a small state file to find what is new.

Facts measured against the live API (2026-10-04) that shape the code:

* Rows carry no acquisition type; the price is the only way to tell paid from free.
* The endpoints are rate-limited hard (HTTP 429 "Try again in N seconds" when calls
  are less than ~1 s apart), so calls are spaced and a 429 waits out its own hint.
* Today's row exists the same day, and older rows can still grow, so every check
  re-reads a short window instead of only "since last time".

The first check only records what already exists. Ringing for months of history the
first time the feature is switched on would be noise, not news.
"""

from __future__ import annotations

import datetime as dt
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from navig_msstore.addons import _BASE, _RESOURCE, DevCenterError, _request

# How far back each check re-reads: late rows land within days, not weeks.
WINDOW_DAYS = 7
# How long a seen row is remembered. Longer than WINDOW_DAYS so a row that leaves the
# window and comes back (a clock change, a longer manual check) is not "new" again.
KEEP_DAYS = 35
# Seconds between calls. Measured: back-to-back calls 429; ~2.5 s apart do not.
CALL_SPACING_S = 2.5
MAX_429_RETRIES = 5
STATE_VERSION = 1

Request = Callable[[str, str, str], Any]


@dataclass(frozen=True)
class Sale:
    """One day's paid acquisitions of one product (an app, or one of its add-ons)."""

    app_id: str
    app_name: str
    product_id: str  # the add-on's id, or the app's own id for a paid app
    product_name: str
    date: str  # YYYY-MM-DD
    quantity: int
    usd: float

    @property
    def key(self) -> str:
        return f"{self.app_id}|{self.product_id}|{self.date}"


def _paced(request: Request, sleep: Callable[[float], None]) -> Request:
    """Wrap a DevCenter call with the spacing and 429 handling the analytics API needs."""

    def call(method: str, url: str, token: str) -> Any:
        for attempt in range(MAX_429_RETRIES + 1):
            sleep(CALL_SPACING_S)
            try:
                return request(method, url, token)
            except DevCenterError as exc:
                if "429" not in str(exc) or attempt == MAX_429_RETRIES:
                    raise
                hint = re.search(r"in (\d+) second", str(exc))
                sleep(int(hint.group(1)) + 1 if hint else 5)
        raise AssertionError("unreachable")  # pragma: no cover

    return call


def list_apps(token: str, *, request: Request = _request) -> list[dict[str, str]]:
    """Every app on the account: ``[{"id", "name"}]``."""
    body = request("GET", f"{_BASE}/applications?top=200", token) or {}
    return [
        {"id": str(a.get("id")), "name": str(a.get("primaryName") or a.get("id"))}
        for a in body.get("value", [])
        if a.get("id")
    ]


def _rows(body: Any) -> list[dict[str, Any]]:
    # The analytics API answers `Value` (capital V); the ingestion API `value`.
    if not isinstance(body, dict):
        return []
    rows = body.get("Value", body.get("value", []))
    return rows if isinstance(rows, list) else []


def paid_sales_from_rows(app: dict[str, str], kind: str, rows: list[dict[str, Any]]) -> list[Sale]:
    """The paid rows of one analytics response, as :class:`Sale` values."""
    out: list[Sale] = []
    for r in rows:
        try:
            usd = float(r.get("purchasePriceUSDAmount") or 0)
            qty = int(r.get("acquisitionQuantity") or 0)
        except (TypeError, ValueError):
            continue
        if usd <= 0 or qty <= 0:
            continue
        if kind == "inappacquisitions":
            product_id = str(r.get("addonProductId") or "addon")
            product_name = str(r.get("inAppProductName") or product_id)
        else:
            product_id, product_name = app["id"], app["name"]
        out.append(
            Sale(
                app_id=app["id"],
                app_name=app["name"],
                product_id=product_id,
                product_name=product_name,
                date=str(r.get("date") or "")[:10],
                quantity=qty,
                usd=round(usd, 2),
            )
        )
    return out


def fetch_sales(
    token: str,
    apps: list[dict[str, str]],
    *,
    days: int = WINDOW_DAYS,
    today: dt.date | None = None,
    request: Request = _request,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Sale]:
    """Paid acquisitions of every app (and its add-ons) over the last *days* days."""
    end = today or dt.date.today()
    start = end - dt.timedelta(days=days)
    call = _paced(request, sleep)
    sales: list[Sale] = []
    for app in apps:
        for kind in ("appacquisitions", "inappacquisitions"):
            url = (
                f"{_RESOURCE}/v1.0/my/analytics/{kind}?applicationId={app['id']}"
                f"&startDate={start}&endDate={end}&aggregationLevel=day&top=10000"
            )
            sales.extend(paid_sales_from_rows(app, kind, _rows(call("GET", url, token))))
    return sales


def empty_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "seeded": False, "seen": {}}


def diff_sales(state: dict[str, Any], sales: list[Sale], *, today: dt.date | None = None) -> tuple[list[Sale], dict[str, Any]]:
    """What grew since *state*, and the state to save next.

    A returned :class:`Sale` carries only the NEW quantity and its share of the money,
    so two more copies sold on a day that already had one read as 2, not 3. Before
    the first save (``seeded`` false) nothing is new: that check sets the baseline.
    """
    seen: dict[str, dict[str, Any]] = dict(state.get("seen") or {})
    seeded = bool(state.get("seeded"))
    new: list[Sale] = []
    for s in sales:
        before = seen.get(s.key) or {}
        prev_qty = int(before.get("qty") or 0)
        if seeded and s.quantity > prev_qty:
            grown = s.quantity - prev_qty
            new.append(Sale(**{**asdict(s), "quantity": grown, "usd": round(s.usd * grown / s.quantity, 2)}))
        if s.quantity >= prev_qty:
            seen[s.key] = {"qty": s.quantity, "usd": s.usd}
    cutoff = ((today or dt.date.today()) - dt.timedelta(days=KEEP_DAYS)).isoformat()
    seen = {k: v for k, v in seen.items() if k.rsplit("|", 1)[-1] >= cutoff}
    return new, {**state, "version": STATE_VERSION, "seeded": True, "seen": seen}


def describe(new: list[Sale]) -> tuple[str, str]:
    """A notification title and body for *new* sales (non-empty)."""
    copies = sum(s.quantity for s in new)
    total = sum(s.usd for s in new)
    if copies == 1:
        s = new[0]
        product = s.product_name if s.product_name == s.app_name else f"{s.app_name} — {s.product_name}"
        return f"💰 New sale: {product}", f"${total:.2f} on the Microsoft Store"
    lines = [
        f"{s.quantity}× {s.product_name if s.product_name == s.app_name else f'{s.app_name} — {s.product_name}'}"
        for s in new
    ]
    return f"💰 {copies} new sales · ${total:.2f}", "\n".join(lines)


# ── State on disk ─────────────────────────────────────────────────────────────


def state_path() -> Path:
    from navig_sdk.host import config_dir

    return config_dir() / "msstore" / "sales_state.json"


def load_state(path: Path | None = None) -> dict[str, Any]:
    """The saved state for a load-modify-SAVE. Raises when the file is locked, so a
    failed read can never become a save that forgets what was already counted."""
    from navig_sdk.files import load_json_for_update

    data = load_json_for_update(path or state_path(), default=None)
    return data if isinstance(data, dict) else empty_state()


def save_state(state: dict[str, Any], path: Path | None = None) -> None:
    from navig_sdk.files import atomic_write_json

    target = path or state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(state, target)


def check(
    token: str,
    *,
    path: Path | None = None,
    today: dt.date | None = None,
    request: Request = _request,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """One full check: read the window, diff it, save. Returns what happened.

    ``{"new": [Sale…], "baseline": bool, "checked": iso, "apps": n}`` — blocking (two
    paced HTTP calls per app); run it off the event loop.
    """
    state = load_state(path)
    apps = list_apps(token, request=request)
    sales = fetch_sales(token, apps, today=today, request=request, sleep=sleep)
    new, next_state = diff_sales(state, sales, today=today)
    checked = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    next_state["last_check"] = checked
    next_state.pop("last_error", None)
    save_state(next_state, path)
    try:
        # The app's chart. Its own file, never the ring's state: a history failure
        # must not stop a sale from ringing.
        record_history(sales, path=history_path() if path is None else path.with_name("sales_history.json"), today=today)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning("msstore: could not update the sales history", exc_info=True)
    return {"new": new, "sales": sales, "baseline": not state.get("seeded"), "checked": checked, "apps": len(apps)}


# ── The daily digest ──────────────────────────────────────────────────────────

# Lines listed under "yesterday" before the rest are counted as "…and N more".
DIGEST_MAX_LINES = 6


def _label(s: Sale) -> str:
    return s.product_name if s.product_name == s.app_name else f"{s.app_name} — {s.product_name}"


def _day_label(date: str) -> str:
    d = dt.date.fromisoformat(date)
    return f"{d:%a} {d.day} {d:%b}"


def _plural(n: int) -> str:
    return f"{n} sale{'s' if n != 1 else ''}"


# How long the per-day totals a summary reported are remembered, to spot late rows.
KNOWN_DAYS = 14
# At most this many "arrived late" lines; the rest are folded into the last one.
LATE_MAX_LINES = 3


def daily_totals(sales: list[Sale], start: dt.date, end: dt.date) -> dict[str, dict[str, Any]]:
    """``{date: {"copies", "usd"}}`` for every day from *start* to *end* (inclusive)."""
    out: dict[str, dict[str, Any]] = {}
    d = start
    while d <= end:
        out[d.isoformat()] = {"copies": 0, "usd": 0.0}
        d += dt.timedelta(days=1)
    for s in sales:
        if s.date in out:
            out[s.date]["copies"] += s.quantity
            out[s.date]["usd"] = round(out[s.date]["usd"] + s.usd, 2)
    return out


def late_sales(totals: dict[str, dict[str, Any]], known: dict[str, Any], before: str) -> list[dict[str, Any]]:
    """Days before *before* whose totals grew since a previous summary reported them.

    Microsoft's report can fill a day in after the fact; a summary that said "no
    sales" for a day that later got some would otherwise stay wrong for good. Days the
    previous summaries never reported are not "late" — they are simply new.
    """
    late: list[dict[str, Any]] = []
    for date in sorted(totals):
        if date >= before:
            continue
        seen = known.get(date)
        if not isinstance(seen, dict):
            continue
        try:
            was_copies, was_usd = int(seen.get("copies") or 0), float(seen.get("usd") or 0)
        except (TypeError, ValueError):
            continue
        now = totals[date]
        if now["copies"] > was_copies:
            late.append({"date": date, "copies": now["copies"] - was_copies, "usd": round(now["usd"] - was_usd, 2)})
    return late


def digest(
    sales: list[Sale],
    *,
    today: dt.date | None = None,
    known: dict[str, Any] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Yesterday's sales, any that arrived late, and the last 7 full days.

    *sales* is the window :func:`check` read (daily totals, not just what was new), so
    the digest costs no extra Partner Center calls. Today is left out: its row is still
    filling up. A day with no sales still gets a digest — it is also the daily proof
    that the watcher is running.

    *known* is what earlier summaries reported per day (``data["known"]`` of the last
    one, merged); a day that has grown since is listed as "arrived late". The returned
    ``data["known"]`` is this summary's own per-day snapshot, to remember for next time.
    """
    day = (today or dt.date.today()) - dt.timedelta(days=1)
    week_start = day - dt.timedelta(days=WINDOW_DAYS - 1)
    yesterday = sorted((s for s in sales if s.date == day.isoformat()), key=lambda s: (-s.usd, _label(s)))
    totals = daily_totals(sales, week_start, day)
    copies, usd = totals[day.isoformat()]["copies"], totals[day.isoformat()]["usd"]
    week_copies = sum(t["copies"] for t in totals.values())
    week_usd = round(sum(t["usd"] for t in totals.values()), 2)
    late = late_sales(totals, known or {}, day.isoformat())

    when = _day_label(day.isoformat())
    if copies:
        title = f"📊 Store sales {when}: {_plural(copies)} · ${usd:.2f}"
        lines = [f"{s.quantity}× {_label(s)} · ${s.usd:.2f}" for s in yesterday[:DIGEST_MAX_LINES]]
        if len(yesterday) > DIGEST_MAX_LINES:
            lines.append(f"…and {len(yesterday) - DIGEST_MAX_LINES} more")
    else:
        title = f"📊 Store sales {when}: no sales"
        lines = []
    for item in late[:LATE_MAX_LINES]:
        lines.append(f"Arrived late: {_plural(item['copies'])} · ${item['usd']:.2f} for {_day_label(item['date'])}")
    if len(late) > LATE_MAX_LINES:
        rest = late[LATE_MAX_LINES:]
        lines.append(
            f"…and {_plural(sum(i['copies'] for i in rest))} · ${sum(i['usd'] for i in rest):.2f} more on earlier days"
        )
    lines.append(f"Last 7 days: {_plural(week_copies)} · ${week_usd:.2f}")
    data = {
        "source": "msstore",
        "date": day.isoformat(),
        "copies": copies,
        "total_usd": usd,
        "week_copies": week_copies,
        "week_usd": week_usd,
        "sales": [asdict(s) for s in yesterday],
        "late": late,
        "known": totals,
    }
    return title, "\n".join(lines), data


def digest_state_path() -> Path:
    from navig_sdk.host import config_dir

    return config_dir() / "msstore" / "digest_state.json"


def load_digest_state(path: Path | None = None) -> dict[str, Any]:
    """``{"last_sent": date, "known": {date: totals}}`` — empty when there is none.

    Raises when the file is locked: a read that failed must not look like "never
    sent", or a restart in the morning would send the day's digest twice."""
    from navig_sdk.files import load_json_for_update

    data = load_json_for_update(path or digest_state_path(), default=None)
    return data if isinstance(data, dict) else {}


def digest_sent_on(path: Path | None = None) -> str | None:
    """The local date the last digest went out, or None (raises when locked)."""
    value = load_digest_state(path).get("last_sent")
    return value if isinstance(value, str) else None


def mark_digest_sent(
    day: dt.date,
    path: Path | None = None,
    *,
    known: dict[str, Any] | None = None,
    previous: dict[str, Any] | None = None,
    entry: dict[str, Any] | None = None,
) -> None:
    """Record the day's summary and what it reported per day (merged with *previous*'s
    ``known``, the last :data:`KNOWN_DAYS` days kept). *entry* (title, body, totals) is
    added to the summaries list the Store sales app shows."""
    from navig_sdk.files import atomic_write_json

    merged = {**((previous or {}).get("known") or {}), **(known or {})}
    cutoff = (day - dt.timedelta(days=KNOWN_DAYS)).isoformat()
    state = {**(previous or {}), "last_sent": day.isoformat(), "known": {d: v for d, v in merged.items() if d >= cutoff}}
    if entry is not None:
        past = [e for e in ((previous or {}).get("history") or []) if isinstance(e, dict)]
        state["history"] = (past + [entry])[-DIGEST_HISTORY_MAX:]
    target = path or digest_state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(state, target)


def digest_due(now: dt.datetime, hour: int, last_sent: str | None) -> bool:
    """Once a day, at the first check at or after *hour* (local time)."""
    return now.hour >= hour and last_sent != now.date().isoformat()


# ── Sales history (the Store sales app's chart) ───────────────────────────────

# How far back the history file keeps daily rows.
HISTORY_DAYS = 400
# What a one-time backfill fetches (one paced call pair per app).
BACKFILL_DAYS = 90
# Summaries kept for the app's "summaries" list.
DIGEST_HISTORY_MAX = 60


def history_path() -> Path:
    from navig_sdk.host import config_dir

    return config_dir() / "msstore" / "sales_history.json"


def load_history(path: Path | None = None) -> dict[str, Any]:
    """``{"rows": {key: sale}, "backfilled_days": n}`` — raises when the file is locked,
    so a failed read can never become a save that drops months of history."""
    from navig_sdk.files import load_json_for_update

    data = load_json_for_update(path or history_path(), default=None)
    if not isinstance(data, dict):
        return {"version": 1, "rows": {}}
    if not isinstance(data.get("rows"), dict):
        data["rows"] = {}
    return data


def merge_history(history: dict[str, Any], sales: list[Sale], *, today: dt.date | None = None) -> dict[str, Any]:
    """Fold daily rows into *history*: a row never shrinks (a smaller re-read is a
    Microsoft correction we do not trust over what was already seen), names refresh,
    and rows older than :data:`HISTORY_DAYS` are dropped."""
    rows: dict[str, Any] = dict(history.get("rows") or {})
    for s in sales:
        before = rows.get(s.key) or {}
        if s.quantity >= int(before.get("quantity") or 0):
            rows[s.key] = asdict(s)
    cutoff = ((today or dt.date.today()) - dt.timedelta(days=HISTORY_DAYS)).isoformat()
    rows = {k: v for k, v in rows.items() if str(v.get("date") or "") >= cutoff}
    return {**history, "version": 1, "rows": rows}


def save_history(history: dict[str, Any], path: Path | None = None) -> None:
    from navig_sdk.files import atomic_write_json

    target = path or history_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(history, target)


def record_history(sales: list[Sale], *, path: Path | None = None, today: dt.date | None = None) -> None:
    """Merge one check's window into the history file."""
    save_history(merge_history(load_history(path), sales, today=today), path)


def backfill_history(
    token: str,
    *,
    days: int = BACKFILL_DAYS,
    path: Path | None = None,
    today: dt.date | None = None,
    request: Request = _request,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Fetch *days* of history into the file (never into the ring's state, so nothing
    old rings). Returns how many rows were read. Blocking; run it off the event loop."""
    apps = list_apps(token, request=request)
    sales = fetch_sales(token, apps, days=days, today=today, request=request, sleep=sleep)
    history = merge_history(load_history(path), sales, today=today)
    history["backfilled_days"] = max(int(history.get("backfilled_days") or 0), days)
    save_history(history, path)
    return len(sales)


def history_series(history: dict[str, Any], *, days: int, today: dt.date | None = None) -> dict[str, Any]:
    """The chart and table for the last *days* days (today included, marked partial)."""
    end = today or dt.date.today()
    start = end - dt.timedelta(days=days - 1)
    rows = [r for r in (history.get("rows") or {}).values() if isinstance(r, dict)]
    series = {d: {"date": d, "copies": 0, "usd": 0.0} for d in (
        (start + dt.timedelta(days=i)).isoformat() for i in range(days)
    )}
    apps: dict[str, dict[str, Any]] = {}
    for r in rows:
        date = str(r.get("date") or "")
        if date not in series:
            continue
        try:
            qty, usd = int(r.get("quantity") or 0), float(r.get("usd") or 0)
        except (TypeError, ValueError):
            continue
        series[date]["copies"] += qty
        series[date]["usd"] = round(series[date]["usd"] + usd, 2)
        app = apps.setdefault(str(r.get("app_id")), {"app": r.get("app_name") or r.get("app_id"), "copies": 0, "usd": 0.0, "products": {}})
        app["copies"] += qty
        app["usd"] = round(app["usd"] + usd, 2)
        prod = app["products"].setdefault(str(r.get("product_id")), {"product": r.get("product_name"), "copies": 0, "usd": 0.0})
        prod["copies"] += qty
        prod["usd"] = round(prod["usd"] + usd, 2)
    by_app = sorted(
        ({**a, "products": sorted(a["products"].values(), key=lambda p: -p["usd"])} for a in apps.values()),
        key=lambda a: -a["usd"],
    )
    dates = sorted(str(r.get("date") or "") for r in rows if r.get("date"))
    return {
        "days": list(series.values()),
        "today": end.isoformat(),
        "apps": by_app,
        "total": {
            "copies": sum(d["copies"] for d in series.values()),
            "usd": round(sum(d["usd"] for d in series.values()), 2),
        },
        "first_date": dates[0] if dates else None,
        "backfilled_days": int(history.get("backfilled_days") or 0),
    }


# ── CLI: `navig mstore sales` ─────────────────────────────────────────────────


def sales_cmd(
    days: int = 30,
    as_json: bool = False,
) -> None:
    """Paid sales (paid apps + add-on purchases) over the last N days. Read-only:
    it never touches the state the daemon's sale alerts use."""
    import json as _json

    import typer

    from navig_msstore.addons import _client

    token = _client(None)
    apps = list_apps(token)
    sales = sorted(fetch_sales(token, apps, days=days), key=lambda s: (s.date, s.app_name), reverse=True)
    if as_json:
        typer.echo(_json.dumps([asdict(s) for s in sales], indent=2))
        return
    total = sum(s.usd for s in sales)
    copies = sum(s.quantity for s in sales)
    if not sales:
        typer.echo(f"No paid sales in the last {days} days across {len(apps)} apps.")
        return
    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:  # plain output without rich
        for s in sales:
            typer.echo(f"  {s.date}  {s.app_name:<28} {s.product_name:<24} {s.quantity:>3}  ${s.usd:.2f}")
    else:
        table = Table(box=None, show_header=True, padding=(0, 2))
        table.add_column("Date", no_wrap=True)
        table.add_column("App", no_wrap=True)
        table.add_column("Product")
        table.add_column("Qty", justify="right", no_wrap=True)
        table.add_column("USD", justify="right", no_wrap=True)
        for s in sales:
            product = "—" if s.product_id == s.app_id else s.product_name
            table.add_row(s.date, s.app_name, product, str(s.quantity), f"${s.usd:.2f}")
        Console().print(table)
    typer.echo(f"{copies} sale(s) · ${total:.2f} in {days} days · the daemon rings for new ones (navig mstore sales --help)")
