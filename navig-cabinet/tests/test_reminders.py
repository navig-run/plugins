"""Expiry reminders: the right thresholds, once each, nothing sensitive in the message."""

from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from navig_cabinet import keys, reminders
from navig_cabinet.ingest import add_path
from navig_cabinet.store import Cabinet


def _file(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / "src" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _add(cab, tmp_path, name, *, expires, category="identity"):
    return add_path(cab, _file(tmp_path, name, os.urandom(40)), read_text=False,
                    category=category, expires=expires).item


def _run(coro):
    return asyncio.run(coro)


class Outbox:
    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    async def __call__(self, title, body):
        self.sent.append((title, body))
        return self.ok


TODAY = date(2026, 9, 27)
NOON = datetime(2026, 9, 27, 12, 0)


def test_the_index_holds_only_id_category_and_date(cab, root, tmp_path):
    _add(cab, tmp_path, "Psychiatric-report.pdf", expires="2026-10-01", category="medical")
    _add(cab, tmp_path, "undated.pdf", expires=None)
    cab.close()
    rows = reminders.read_index(root)
    assert len(rows) == 1 and set(rows[0]) == {"id", "category", "expires"}
    assert b"Psychiatric" not in (root / reminders.INDEX_FILE).read_bytes()


def test_the_index_follows_edits_and_the_trash(cab, root, tmp_path):
    item = _add(cab, tmp_path, "passport.pdf", expires="2027-01-01")
    cab.update(item.id, expires="2031-01-01")
    cab.close()
    assert reminders.read_index(root)[0]["expires"] == "2031-01-01"
    with Cabinet.open(root) as c:
        c.set_state(item.id, "trashed")
    assert reminders.read_index(root) == []


@pytest.mark.parametrize("days,level", [(95, None), (90, "90"), (45, "90"), (30, "30"),
                                        (8, "30"), (7, "7"), (0, "7"), (-3, "expired")])
def test_thresholds(days, level):
    row = {"id": "a1", "category": "identity", "expires": (TODAY + timedelta(days=days)).isoformat()}
    batch = reminders.due([row], {}, TODAY)
    assert (batch[0][2].rsplit("|", 1)[1] if batch else None) == level


def test_each_threshold_is_sent_once_and_a_renewal_starts_over(cab, root, tmp_path):
    item = _add(cab, tmp_path, "passport.pdf", expires=(TODAY + timedelta(days=20)).isoformat())
    cab.close()
    out = Outbox()
    assert _run(reminders.check(root, now=NOON, dispatch=out))["sent"] == 1
    assert _run(reminders.check(root, now=NOON, dispatch=out, force=True))["sent"] == 0
    # renewed: a new expiry date is a new document as far as reminders go
    with Cabinet.open(root) as c:
        c.update(item.id, expires=(TODAY + timedelta(days=25)).isoformat())
    assert _run(reminders.check(root, now=NOON, dispatch=out, force=True))["sent"] == 1
    assert len(out.sent) == 2


def test_the_message_names_the_category_never_the_title(cab, root, tmp_path):
    item = _add(cab, tmp_path, "HIV-test-result.pdf", expires=(TODAY + timedelta(days=5)).isoformat(),
                category="medical")
    cab.close()
    out = Outbox()
    _run(reminders.check(root, now=NOON, dispatch=out))
    title, body = out.sent[0]
    assert "HIV" not in title + body
    assert "A medical document expires in 5 day(s)" in body and item.id in body


def test_once_a_day_after_the_configured_hour(cab, root, tmp_path):
    _add(cab, tmp_path, "id.pdf", expires=(TODAY + timedelta(days=3)).isoformat())
    cab.close()
    out = Outbox()
    early = datetime(2026, 9, 27, 7, 0)
    assert _run(reminders.check(root, now=early, dispatch=out))["skipped"] == "not due yet today"
    assert _run(reminders.check(root, now=NOON, dispatch=out))["sent"] == 1
    assert _run(reminders.check(root, now=NOON + timedelta(hours=2), dispatch=out))["skipped"]
    assert len(out.sent) == 1


def test_a_failed_delivery_is_not_marked_sent_and_retries_hourly(cab, root, tmp_path):
    _add(cab, tmp_path, "id.pdf", expires=(TODAY + timedelta(days=3)).isoformat())
    cab.close()
    down = Outbox(ok=False)
    assert _run(reminders.check(root, now=NOON, dispatch=down))["pending"] == 1
    assert _run(reminders.check(root, now=NOON + timedelta(minutes=5), dispatch=down))["skipped"] == "retrying later"
    up = Outbox()
    assert _run(reminders.check(root, now=NOON + timedelta(hours=1, minutes=1), dispatch=up))["sent"] == 1


def test_turning_reminders_off_deletes_the_index(cab, root, tmp_path, monkeypatch):
    _add(cab, tmp_path, "id.pdf", expires="2027-01-01")
    cab.close()
    assert (root / reminders.INDEX_FILE).exists()
    monkeypatch.setattr(reminders, "_config", lambda: {"enabled": "false"})  # a raw config string
    with Cabinet.open(root) as c:
        c.update(c.items()[0].id, title="x")
    assert not (root / reminders.INDEX_FILE).exists()


def test_a_passphrase_cabinet_still_reminds_without_the_passphrase(cab, root, tmp_path):
    _add(cab, tmp_path, "id.pdf", expires=(TODAY + timedelta(days=2)).isoformat())
    keys.set_passphrase(root, cab.master_key, "pw")
    cab.close()
    out = Outbox()
    assert _run(reminders.check(root, now=NOON, dispatch=out))["sent"] == 1


def test_another_machines_index_reminds_nothing(cab, root, tmp_path, monkeypatch):
    _add(cab, tmp_path, "id.pdf", expires=(TODAY + timedelta(days=2)).isoformat())
    cab.close()
    monkeypatch.setattr(keys, "machine_material", lambda: (b"other-machine", "machine-id"))
    assert reminders.read_index(root) == []


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_remind_dry_run_sends_nothing_and_reports_due(tmp_path):
    import json

    from navig_cabinet.commands.cabinet import cabinet_app
    from typer.testing import CliRunner

    runner = CliRunner()
    src = _file(tmp_path, "passport.pdf", b"p")
    soon = (date.today() + timedelta(days=10)).isoformat()
    assert runner.invoke(cabinet_app, ["add", str(src), "--no-ocr", "--expires", soon]).exit_code == 0
    res = runner.invoke(cabinet_app, ["remind", "--dry-run", "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output[res.output.index("{"):])
    assert out["due"] == 1 and out["sent"] == 0 and "identity document" in out["body"]
    assert "passport" not in out["body"].lower()
    status = runner.invoke(cabinet_app, ["status", "--json"])
    assert json.loads(status.output[status.output.index("{"):])["reminders"] is True
