"""``navig email watch`` — one incremental pass over new mail, rules applied.

Cursor first, window second. Gmail's ``history.list`` gives every message added
since the last ``historyId`` — cheap, exact, and it never re-reads the mailbox. When
the cursor is too old (Gmail answers 404) the pass falls back to a time-window search
minus the ids it has already seen. The very first pass only *seeds*: it records the
cursor and the current window as seen and notifies nobody — a fresh install must not
announce two years of backlog.

Delivery integrity (same class as the daemon's monitor): Telegram lines are sent
once per pass; if that send fails, the cursor is NOT advanced, so the next pass
retries instead of dropping the alert.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from navig_email.errors import ConnectorAPIError

from . import account, actions, messages, rules as R, state as S
from .space import MailroomPaths

SEED_WINDOW = "2d"
WINDOW_LIMIT = 500


@dataclass
class WatchResult:
    seeded: bool = False
    source: str = ""  # history | window
    candidates: int = 0  # ids Gmail reported since the cursor
    new: int = 0  # not seen before
    matched: int = 0  # messages with ≥1 rule hit
    outcome: actions.Outcome = field(default_factory=actions.Outcome)
    notified: bool | None = None
    dry_run: bool = False
    cursor_advanced: bool = False


async def _history_ids(connector, start: str) -> tuple[list[str], str]:
    ids: list[str] = []
    seen: set[str] = set()
    latest = start
    token: str | None = None
    while True:
        data = await connector.list_history(
            start, history_types=("messageAdded",), page_token=token
        )
        for h in data.get("history") or []:
            for added in h.get("messagesAdded") or []:
                m = added.get("message") or {}
                mid = str(m.get("id", ""))
                if mid and mid not in seen and "DRAFT" not in (m.get("labelIds") or []):
                    seen.add(mid)
                    ids.append(mid)
        latest = str(data.get("historyId") or latest)
        token = data.get("nextPageToken")
        if not token:
            break
    return ids, latest


async def run(
    paths: MailroomPaths,
    rule_list: list[dict[str, Any]],
    *,
    dry_run: bool = False,
    notify_title: str = "Courrier électronique",
) -> WatchResult:
    res = WatchResult(dry_run=dry_run)
    c = await account.connector()
    prof = await account.profile(c)
    st = S.load(paths)

    # A different account than the one the state was built for: start over.
    if st.get("account") and prof["email"] and st["account"] != prof["email"]:
        st = S.load(MailroomPaths(paths.space_root))  # defaults
        st["seeded"] = False

    if not st.get("seeded") or not st.get("history_id"):
        entries = await c.iter_message_ids(
            f"newer_than:{SEED_WINDOW}", limit=WINDOW_LIMIT, include_spam_trash=True
        )
        if not dry_run:
            S.remember(st, [e["id"] for e in entries])
            st.update(
                history_id=prof["history_id"],
                seeded=True,
                account=prof["email"],
                last_watch=S.now_iso(),
                last_error="",
            )
            S.save(paths, st)
        res.seeded = True
        res.candidates = len(entries)
        return res

    try:
        ids, latest = await _history_ids(c, st["history_id"])
        res.source = "history"
    except ConnectorAPIError as exc:
        if exc.status_code not in (400, 404):
            raise
        entries = await c.iter_message_ids(
            f"newer_than:{SEED_WINDOW}", limit=WINDOW_LIMIT, include_spam_trash=True
        )
        ids, latest = [e["id"] for e in entries], prof["history_id"]
        res.source = "window"

    res.candidates = len(ids)
    seen = set(st.get("seen_ids") or [])
    fresh = [i for i in ids if i not in seen]
    res.new = len(fresh)

    fmt = "full" if R.needs_body(rule_list) else "metadata"
    raw = await c.get_many(fresh, format=fmt) if fresh else []
    labels = actions.LabelCache(c, st.get("label_ids"))
    out = res.outcome
    handled: list[str] = []
    with_notify: set[str] = set()

    for m in raw:
        body = c._extract_body(m.get("payload") or {}) if fmt == "full" else ""
        msg = messages.shape(m, body=body)
        matched = R.all_matches(msg, rule_list)
        if matched:
            res.matched += 1
        before = len(out.notifications)
        for rule in matched:
            try:
                acts = R.actions_for(rule)
            except ValueError as exc:
                out.errors.append(f"rule {rule.get('id')}: {exc}")
                continue
            await actions.apply(
                c, msg, rule, acts, labels=labels, dry_run=dry_run, out=out
            )
        if len(out.notifications) > before:
            with_notify.add(msg["id"])
        handled.append(msg["id"])

    text = actions.notification_text(out.notifications, title=notify_title)
    if text and not dry_run:
        try:
            from navig.messaging.notify_operator import notify_operator
        except ImportError:
            # Standalone: there is no Telegram bot to deliver through. Same outcome as a
            # failed delivery — the alerts are kept for a pass that can send them — and
            # every label/action above has already been applied.
            res.notified = False
            out.errors.append(
                "Telegram alerts need navig (pip install navig) — alerts kept for the next pass"
            )
        else:
            res.notified = notify_operator(text)
            if not res.notified:
                out.errors.append(
                    "Telegram delivery failed — alerts kept for the next pass"
                )
        if not res.notified:
            handled = [h for h in handled if h not in with_notify]

    if not dry_run:
        S.remember(st, handled)
        if res.notified is not False:
            st["history_id"] = latest
            res.cursor_advanced = True
        st["label_ids"] = labels.ids
        st["last_watch"] = S.now_iso()
        st["last_error"] = "; ".join(out.errors[:3])
        S.save(paths, st)
    return res
