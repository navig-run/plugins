"""Execute a rule's actions on one message. The only place the mailroom *changes* Gmail.

``label`` / ``star`` / ``archive`` / ``mark_read`` are one ``messages.modify`` each;
``notify`` is collected and sent once per run as a single Telegram message (a batch of
new mail must not become twenty pings); ``run`` executes a ``navig …`` argv without a
shell. ``dry_run`` reports every intended change and performs none.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from .messages import address, display_name
from .rules import Action, render_run

logger = logging.getLogger("navig_email.actions")

SNIPPET_CHARS = 120


@dataclass
class Outcome:
    """What one run did (or would do)."""

    labelled: int = 0
    starred: int = 0
    archived: int = 0
    marked_read: int = 0
    ran: int = 0
    notifications: list[str] = field(default_factory=list)  # Telegram lines, sent once
    planned: list[str] = field(default_factory=list)  # human log, one per action
    errors: list[str] = field(default_factory=list)

    @property
    def changes(self) -> int:
        return (
            self.labelled + self.starred + self.archived + self.marked_read + self.ran
        )


class LabelCache:
    """Label name → id, creating missing labels once per run."""

    def __init__(self, connector, known: dict[str, str] | None = None):
        self._c = connector
        self.ids: dict[str, str] = dict(known or {})

    async def id_for(self, name: str) -> str:
        key = name.strip()
        if key in self.ids:
            return self.ids[key]
        lid = await self._c.ensure_label(key)
        self.ids[key] = lid
        return lid


RUN_TIMEOUT = 300


async def _run_navig(argv: list[str]) -> tuple[int, str]:
    """Run a ``navig …`` argv off the event loop; ``(returncode, decoded output tail)``.

    A subprocess, never a shell; captured as BYTES and decoded by the console helper
    because a navig verb's child output is written in whatever codec the tool chose,
    not the locale code page.
    """
    try:
        from navig.core.proc_text import decode_console_output
    except ImportError:  # no navig: this runs `navig …` anyway, so the error surfaces below
        def decode_console_output(raw):  # type: ignore[misc]
            return (raw or b"").decode("utf-8", "replace")

    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=RUN_TIMEOUT)
    except (asyncio.TimeoutError, TimeoutError):
        proc.kill()
        raise TimeoutError(f"timed out after {RUN_TIMEOUT}s") from None
    return int(proc.returncode or 0), decode_console_output(raw)


def notify_line(msg: dict[str, Any], rule: dict[str, Any]) -> str:
    """One Telegram line: rule · sender · subject · ≤120-char snippet. HTML-escaped."""
    from navig_email._compat import escape_html as esc

    who = display_name(msg.get("from", "")) or address(msg.get("from", "")) or "?"
    subject = (msg.get("subject") or "(sans objet)")[:120]
    snippet = (msg.get("snippet") or "")[:SNIPPET_CHARS]
    tag = rule.get("name") or rule.get("id") or "règle"
    line = f"• <b>{esc(tag)}</b> — {esc(who)} · {esc(subject)}"
    if snippet:
        line += f"\n  <i>{esc(snippet)}</i>"
    return line


async def apply(
    connector,
    msg: dict[str, Any],
    rule: dict[str, Any],
    actions: list[Action],
    *,
    labels: LabelCache,
    dry_run: bool,
    out: Outcome,
) -> None:
    mid = msg.get("id", "")
    for act in actions:
        desc = f"{mid[:10]} {act}"
        if act.kind == "notify":
            out.notifications.append(notify_line(msg, rule))
            out.planned.append(desc)
            continue
        if act.kind == "run":
            argv = render_run(act.arg, msg)
            out.planned.append(f"{mid[:10]} run {' '.join(argv)}")
            if dry_run:
                continue
            try:
                code, tail = await _run_navig(argv)
                if code != 0:
                    out.errors.append(f"{mid[:10]} run exit {code}: {tail[-300:]}")
                else:
                    out.ran += 1
            except (OSError, asyncio.TimeoutError, TimeoutError) as exc:
                out.errors.append(f"{mid[:10]} run failed: {exc}")
            continue

        add: list[str] = []
        remove: list[str] = []
        if act.kind == "label":
            add.append(await labels.id_for(act.arg)) if not dry_run else add.append(
                f"<{act.arg}>"
            )
        elif act.kind == "star":
            add.append("STARRED")
        elif act.kind == "archive":
            remove.append("INBOX")
        elif act.kind == "mark_read":
            remove.append("UNREAD")
        out.planned.append(desc)
        if dry_run:
            continue
        res = await connector.modify_message(
            mid, add=add or None, remove=remove or None
        )
        if not res.success:
            out.errors.append(f"{mid[:10]} {act}: {res.error}")
            continue
        if act.kind == "label":
            out.labelled += 1
        elif act.kind == "star":
            out.starred += 1
        elif act.kind == "archive":
            out.archived += 1
        elif act.kind == "mark_read":
            out.marked_read += 1


def notification_text(lines: list[str], *, title: str) -> str:
    from navig_email._compat import escape_html as esc

    if not lines:
        return ""
    head = f"📧 <b>{esc(title)}</b> — {len(lines)} message(s)"
    return "\n".join(
        [head, *lines[:20]] + ([f"… +{len(lines) - 20}"] if len(lines) > 20 else [])
    )
