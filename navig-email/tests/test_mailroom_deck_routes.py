"""The Mailroom deck routes: the payload shape, the action allowlist, the guards.

Sync tests driving ``asyncio.run`` — the same shape as this plugin's other suites, which
run without an asyncio-auto pytest config.

The action endpoint is the one that can start a process, so it gets the most attention: an
unknown name must be refused, and the argv must come from the table rather than from
anything the caller sent.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("aiohttp")

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from navig_email import overview as O  # noqa: E402
from navig_email.deck_routes import email as R  # noqa: E402
from navig_email.space import MailroomPaths  # noqa: E402

LEDGER_ROWS = [
    {
        "sha256": "a",
        "emetteur_label": "CAF",
        "echeance": "2026-10-02",
        "action": "fournir",
        "dest_rel": "personal/logement/2026/a.pdf",
        "original_name": "a.pdf",
    },
    {
        "sha256": "b",
        "emetteur_label": "EDF",
        "echeance": "",
        "action": "payer",
        "dest_rel": "personal/energie-telecom/2026/b.pdf",
        "original_name": "b.pdf",
    },
]
RADAR_ROWS = [
    {
        "id": "late",
        "due": "2026-09-10",
        "organisme": "CAF",
        "objet": "APL",
        "status": "open",
    },
    {
        "id": "soon",
        "due": "2026-09-24",
        "organisme": "EDF",
        "objet": "facture",
        "status": "open",
    },
    {
        "id": "far",
        "due": "2027-01-01",
        "organisme": "X",
        "objet": "later",
        "status": "open",
    },
    {
        "id": "done",
        "due": "2026-09-21",
        "organisme": "Y",
        "objet": "closed",
        "status": "done",
    },
]


def _jsonl(rows) -> str:
    return "\n".join(json.dumps(r) for r in rows) + "\n"


@pytest.fixture
def space(tmp_path) -> Path:
    root = tmp_path / "paperwork-space"
    (root / ".navig").mkdir(parents=True)
    (root / "mailroom" / "ledger").mkdir(parents=True)
    (root / "inbox").mkdir()
    (root / ".navig" / "config.yaml").write_text(
        "mailroom:\n  edge:\n    url: https://edge.example.dev\n", encoding="utf-8"
    )
    (root / "mailroom" / "ledger" / "courrier.jsonl").write_text(
        _jsonl(LEDGER_ROWS), encoding="utf-8"
    )
    (root / "mailroom" / "echeances.jsonl").write_text(
        _jsonl(RADAR_ROWS), encoding="utf-8"
    )
    (root / "inbox" / "IMG_1.jpg").write_bytes(b"x")
    return root


@pytest.fixture
def pinned(space, monkeypatch) -> MailroomPaths:
    paths = MailroomPaths(space)
    monkeypatch.setattr(O, "resolve_space", lambda s=None: paths)
    return paths


def _app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/deck/email/mailroom/overview", R.handle_mailroom_overview)
    app.router.add_get(
        "/api/deck/email/mailroom/edge/events", R.handle_mailroom_edge_events
    )
    app.router.add_get(
        "/api/deck/email/mailroom/paper/ledger", R.handle_mailroom_paper_ledger
    )
    app.router.add_post("/api/deck/email/mailroom/action", R.handle_mailroom_action)
    return app


def call(method: str, path: str, **kw) -> tuple[int, dict]:
    """One request against the mailroom routes → ``(status, json)``."""

    async def _go():
        async with TestClient(TestServer(_app())) as client:
            resp = await client.request(method, path, **kw)
            return resp.status, await resp.json()

    return asyncio.run(_go())


def test_overview_has_every_section_and_survives_a_dead_edge(pinned, monkeypatch):
    from navig_email import edge as E

    def boom(paths):
        raise RuntimeError("edge down")

    monkeypatch.setattr(E, "health", boom)
    monkeypatch.setattr(
        O,
        "cron_section",
        lambda: {
            "jobs": [
                {
                    "id": "job_44",
                    "name": "courrier:scan",
                    "schedule": "every 30 minutes",
                    "enabled": True,
                    "next_run": "2026-09-26T18:00:00",
                    "last_run": "",
                }
            ]
        },
    )
    status, body = call("GET", "/api/deck/email/mailroom/overview")
    assert status == 200 and body["ok"] is True
    data = body["data"]
    assert {"edge", "gmail", "paper", "cron"} <= set(data)
    # The edge failed; the page did not.
    assert data["edge"]["configured"] is True and "edge down" in data["edge"]["error"]
    assert data["paper"]["filed_total"] == 2 and data["paper"]["inbox_pending"] == 1
    assert data["paper"]["radar"] and data["cron"]["jobs"][0]["name"] == "courrier:scan"
    assert isinstance(data["gmail"]["piratebay"]["total"], int)


def test_paper_ledger_paging_and_bad_limit(pinned):
    status, body = call("GET", "/api/deck/email/mailroom/paper/ledger?limit=1")
    assert status == 200 and len(body["data"]["filed"]) == 1
    assert body["data"]["filed"][0]["emetteur_label"] == "EDF"  # newest first

    status, body = call("GET", "/api/deck/email/mailroom/paper/ledger?limit=abc")
    assert status == 400 and "limit" in body["error"]


def test_edge_events_uses_the_plugin_client(pinned, monkeypatch):
    from navig_email import edge as E

    seen: dict = {}

    def fake_events(p, *, limit=50):
        seen["limit"] = limit
        return [{"ts": "2026-09-26T10:00:00Z", "alias": "support", "subject": "s"}]

    monkeypatch.setattr(E, "events", fake_events)
    status, body = call("GET", "/api/deck/email/mailroom/edge/events?limit=5")
    assert status == 200 and seen["limit"] == 5
    assert body["data"]["events"][0]["alias"] == "support"


def test_unknown_action_is_refused_before_anything_runs(pinned):
    status, body = call(
        "POST", "/api/deck/email/mailroom/action", json={"action": "rm -rf /"}
    )
    assert status == 400 and "unknown action" in body["error"]
    status, _ = call("POST", "/api/deck/email/mailroom/action", json={})
    assert status == 400


class _Proc:
    def __init__(self, code: int, out: bytes):
        self.returncode = code
        self._out = out

    async def communicate(self):
        return self._out, b""

    def kill(self):
        pass


def test_a_known_action_runs_its_own_argv_and_returns_the_json(pinned, monkeypatch):
    seen: dict = {}

    async def fake_exec(*argv, **kw):
        seen["argv"] = list(argv)
        return _Proc(0, b'{"migrated": 1}')

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    status, body = call(
        "POST", "/api/deck/email/mailroom/action", json={"action": "scan"}
    )
    assert status == 200
    assert body["data"]["result"] == {"migrated": 1} and body["data"]["exit_code"] == 0
    # argv comes from the table, with only {space} substituted.
    assert seen["argv"][:3] == ["navig", "paperwork", "scan"]
    assert str(pinned.space_root) in seen["argv"]
    assert all(";" not in part and "&" not in part for part in seen["argv"])


def test_a_failing_action_reports_the_exit_code(pinned, monkeypatch):
    async def fake_exec(*argv, **kw):
        return _Proc(2, b"boom")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    status, body = call(
        "POST", "/api/deck/email/mailroom/action", json={"action": "watch"}
    )
    assert status == 502 and "exited 2" in body["error"]


def test_radar_from_jsonl_orders_and_labels(space):
    rows = O._radar_from_jsonl(
        space / "mailroom" / "echeances.jsonl", horizon=30, today=date(2026, 9, 19)
    )
    assert [r["objet"] for r in rows] == [
        "APL",
        "facture",
    ]  # 'far' out of horizon, 'done' closed
    assert rows[0]["status"] == "EN RETARD" and rows[0]["days"] == -9
    assert rows[1]["status"] == "URGENT"


def test_action_table_only_runs_navig_verbs():
    for name, template in R.MAILROOM_ACTIONS.items():
        assert template[0] == "navig", name
        assert "{space}" in template, name
        assert all(";" not in part and "|" not in part for part in template), name
