"""A briefing as Telegram-sized messages: one per section, plain text, link buttons per item.

Buttons here are plain links (open the product, register, the map, a pre-filled calendar
entry, apply) — nothing is booked, registered or saved by pressing them. A calendar button
opens Google Calendar's own form for the person to confirm; no event is created for them.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import urlencode

from navig_email.brief.model import Item
from navig_email.brief.synthesize import SECTION_ORDER, SECTION_TITLES

TELEGRAM_LIMIT = 3900  # a margin under Telegram's 4096


def _money(r: dict[str, Any] | None, unit: str) -> str:
    if not r:
        return ""
    lo, hi = r.get("min"), r.get("max")

    def fmt(v: int) -> str:
        return f"€{v // 1000}k" if unit == "year" and v >= 1000 and v % 1000 == 0 else f"€{v:,}".replace(",", " ")

    if lo and hi and lo != hi:
        return f"{fmt(lo)}–{fmt(hi)} / {unit}"
    if hi and not lo:
        return f"up to {fmt(hi)} / {unit}"
    if lo and not hi:
        return f"from {fmt(lo)} / {unit}"
    return f"{fmt(hi or lo)} / {unit}"


def calendar_url(it: Item) -> str:
    f = it.fields
    if not f.get("date") or not f.get("start"):
        return ""
    d = str(f["date"]).replace("-", "")
    start, end = str(f["start"]).replace(":", ""), str(f.get("end") or f["start"]).replace(":", "")
    q = {"action": "TEMPLATE", "text": it.title, "dates": f"{d}T{start}00/{d}T{end}00",
         "location": ", ".join(x for x in (f.get("venue"), f.get("address")) if x)}
    if f.get("timezone"):
        q["ctz"] = f["timezone"]
    return "https://calendar.google.com/calendar/render?" + urlencode(q)


def item_text(it: Item) -> str:
    f = it.fields
    lines: list[str] = []
    if it.kind == "product":
        lines.append(f"• {it.title}" + (f" — {it.summary}" if it.summary else ""))
        if f.get("description") and f.get("description") != it.summary:
            lines.append(f"  {f['description']}")
        meta = " · ".join(x for x in (f"Category: {f['category']}" if f.get("category") else "",
                                       f"Platform: {f['platform']}" if f.get("platform") else "") if x)
        if meta:
            lines.append(f"  {meta}")
    elif it.kind == "event":
        lines.append(f"• {it.title}")
        when = str(f.get("date") or "")
        try:
            when = date.fromisoformat(when).strftime("%A %d %B %Y")
        except ValueError:
            pass
        hours = f"{f['start']}–{f['end']}" if f.get("start") and f.get("end") else str(f.get("start") or "")
        tz = f" ({f['timezone']}{', ' + f['timezone_source'] if f.get('timezone_source') else ''})" if f.get("timezone") else ""
        lines.append(f"  {when}" + (f" · {hours}{tz}" if hours else ""))
        if f.get("venue") or f.get("address"):
            lines.append("  " + ", ".join(x for x in (f.get("venue"), f.get("address")) if x))
        if f.get("topics"):
            lines.append("  Topics: " + ", ".join(f["topics"]))
        lines.append(f"  Registration: {f.get('registration') or 'Not confirmed'}")
        if f.get("deadline"):
            lines.append(f"  Register by: {f['deadline']}")
        if f.get("date_warning"):
            lines.append(f"  ⚠ {f['date_warning']}")
    elif it.kind == "job":
        head = f"• {it.title}" + (f" — {f['location']}" if f.get("location") else "")
        if f.get("contract"):
            head += f" · {f['contract']}"
        lines.append(head)
        pay = " · ".join(x for x in (_money(f.get("salary_year"), "year"), _money(f.get("day_rate"), "day")) if x)
        if pay:
            lines.append(f"  {pay}")
    else:
        lines.append(f"• {it.title}")
        if it.summary:
            lines.append(f"  {it.summary[:220]}")
    if it.reasons and it.kind != "article":
        lines.append("  Why: " + "; ".join(it.reasons))
    return "\n".join(lines)


def buttons(it: Item) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if it.kind == "product" and it.links.get("primary"):
        out.append({"text": f"Visit {it.title[:24]}", "url": it.links["primary"]})
    elif it.kind == "event":
        if it.links.get("register"):
            out.append({"text": "Register", "url": it.links["register"]})
        if it.links.get("maps"):
            out.append({"text": "Maps", "url": it.links["maps"]})
        cal = calendar_url(it)
        if cal:
            out.append({"text": "Calendar", "url": cal})
    elif it.kind == "job" and (it.links.get("apply") or it.links.get("primary")):
        out.append({"text": f"Offer: {it.title[:22]}", "url": it.links.get("apply") or it.links["primary"]})
    elif it.links.get("primary"):
        out.append({"text": f"Read: {it.title[:24]}", "url": it.links["primary"]})
    return out


def messages(sections: dict[str, list[Item]], *, title: str, actions: bool = False,
             ) -> list[tuple[str, str, list[list[dict[str, str]]]]]:
    """``[(title, text, keyboard_rows)]`` — one message per section, each within Telegram's limit.

    *actions* adds Save / Dismiss buttons (server-side tokens) — only for a briefing that is
    actually being delivered, never for a dry run.
    """
    out = []
    for kind in SECTION_ORDER:
        items = sections.get(kind) or []
        if not items:
            continue
        head = f"{title} — {SECTION_TITLES[kind]}"
        body, rows = "", []
        for it in items:
            chunk = item_text(it) + "\n\n"
            if len(body) + len(chunk) > TELEGRAM_LIMIT:
                out.append((head, body.rstrip(), rows))
                body, rows = "", []
            body += chunk
            btns = buttons(it)
            if btns:
                rows.append(btns[:3])
            if actions and it.kind != "article":
                from navig_email import tg

                rows.append([dict(b, text=f"{b['text']} · {it.title[:18]}") for b in tg.item_buttons(it.fingerprint(), it.kind)])
        out.append((head, body.rstrip(), rows))
    return out
