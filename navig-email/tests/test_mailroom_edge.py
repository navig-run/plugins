"""navig ↔ the edge Worker: config resolution, bearer auth, send-as guard, CLI exit codes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from navig_email import edge as E  # noqa: E402
from navig_email.commands.email import email_app  # noqa: E402
from navig_email.space import MailroomPaths  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

runner = CliRunner()
URL = "https://cybesis-mailroom.example.workers.dev"


@pytest.fixture
def space(tmp_path) -> Path:
    root = tmp_path / "space"
    (root / ".navig").mkdir(parents=True)
    (root / ".navig" / "config.yaml").write_text(
        f"mailroom:\n  edge:\n    url: {URL}\n    worker: cybesis-mailroom\n    domain: cybesis.com\n",
        encoding="utf-8",
    )
    return root


class _Resp:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def http(monkeypatch):
    calls: list[tuple[str, str, dict]] = []
    # Patch the vault accessor, not edge_token: reads go through read_token() and a test
    # must never reach the real vault (it did, and printed a live token into the failure).
    monkeypatch.setattr(E, "_vault_secret", lambda label: "tok-123")

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(("GET", url, {"params": params, "headers": headers}))
        if url.endswith("/health"):
            return _Resp(
                200, {"ok": True, "worker": "cybesis-mailroom", "sending": True}
            )
        if url.endswith("/stats"):
            return _Resp(
                200,
                {
                    "days": params["days"],
                    "total": 2,
                    "forwarded": 2,
                    "notified": 1,
                    "by_alias": {"support": 2},
                    "by_tag": {"support": 2, "facture": 1},
                    "by_day": {"2026-09-20": 2},
                    "top_domains": [["x.io", 2]],
                },
            )
        if url.endswith("/events"):
            return _Resp(
                200,
                {
                    "events": [
                        {
                            "ts": "2026-09-20T10:00:00Z",
                            "alias": "support",
                            "from_name": "A",
                            "subject": "S",
                            "tags": "support",
                            "forwarded": 1,
                            "notified": 1,
                        }
                    ]
                },
            )
        return _Resp(404, {"error": "not found"})

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(("POST", url, {"json": json, "headers": headers}))
        if not str(json.get("from", "support@cybesis.com")).endswith("@cybesis.com"):
            return _Resp(400, {"error": "from must be an @cybesis.com address"})
        return _Resp(200, {"ok": True, "result": {"messageId": "<m@cybesis.com>"}})

    monkeypatch.setattr(E.httpx, "get", fake_get)
    monkeypatch.setattr(E.httpx, "post", fake_post)
    return calls


def test_edge_url_from_space_config_and_missing(space, tmp_path):
    assert E.edge_url(MailroomPaths(space)) == URL
    bare = tmp_path / "bare"
    (bare / ".navig").mkdir(parents=True)
    with pytest.raises(E.EdgeNotConfigured):
        E.edge_url(MailroomPaths(bare))


def test_calls_carry_the_bearer_and_send_threads(space, http):
    paths = MailroomPaths(space)
    assert E.health(paths)["sending"] is True
    st = E.stats(paths, days=3)
    assert st["total"] == 2 and http[-1][2]["params"] == {"days": 3}
    assert http[-1][2]["headers"]["Authorization"] == "Bearer tok-123"
    assert E.events(paths, limit=5)[0]["alias"] == "support"
    res = E.send(
        paths, to="a@b.c", subject="Re: x", text="hi", in_reply_to="<orig@b.c>"
    )
    assert res["ok"] and http[-1][2]["json"]["in_reply_to"] == "<orig@b.c>"
    with pytest.raises(RuntimeError):
        E.send(paths, to="a@b.c", subject="x", text="y", from_addr="me@gmail.com")


def test_stats_renderers(space):
    st = {
        "days": 7,
        "total": 3,
        "forwarded": 3,
        "notified": 2,
        "by_alias": {"support": 2, "spam": 1},
        "by_tag": {"support": 2, "facture": 1},
        "top_domains": [["x.io", 2]],
    }
    md = E.stats_markdown(st, url=URL)
    assert "| support@ | 2 |" in md and "| x.io | 2 |" in md
    tg = E.stats_telegram(st)
    assert "support@ 2" in tg and "#facture 1" in tg


def _run(*args):
    return runner.invoke(email_app, list(args))


def test_cli_edge_status_stats_events(space, http):
    res = _run("edge", "status", "--space", str(space))
    assert res.exit_code == 0 and "sending=yes" in res.output
    res = _run("edge", "stats", "--space", str(space), "--days", "3", "--json")
    assert res.exit_code == 0 and '"total": 2' in res.output
    res = _run("edge", "events", "--space", str(space), "-n", "1")
    assert res.exit_code == 0 and "support@" in res.output


def test_cli_send_via_cloudflare_guards_and_sends(space, http):
    res = _run(
        "send",
        "--via",
        "cloudflare",
        "--to",
        "a@b.c",
        "--subject",
        "s",
        "--body",
        "b",
        "--space",
        str(space),
    )
    assert res.exit_code == 2  # --yes required
    res = _run(
        "send",
        "--via",
        "cloudflare",
        "--to",
        "a@b.c",
        "--subject",
        "s",
        "--body",
        "b",
        "--draft",
        "--yes",
    )
    assert res.exit_code == 2  # drafts are Gmail-only
    res = _run(
        "send",
        "--via",
        "cloudflare",
        "--to",
        "a@b.c",
        "--subject",
        "s",
        "--body",
        "b",
        "--space",
        str(space),
        "--yes",
    )
    assert res.exit_code == 0 and "Sent to a@b.c" in res.output
    res = _run(
        "edge",
        "send",
        "--to",
        "a@b.c",
        "--subject",
        "s",
        "--body",
        "b",
        "--space",
        str(space),
        "--yes",
        "--from",
        "legal@cybesis.com",
    )
    assert res.exit_code == 0 and http[-1][2]["json"]["from"] == "legal@cybesis.com"


def test_cli_edge_unconfigured_exits_1(tmp_path):
    bare = tmp_path / "bare"
    (bare / ".navig").mkdir(parents=True)
    res = _run("edge", "status", "--space", str(bare))
    assert res.exit_code == 1 and "edge URL" in res.output


ACCOUNTS_YAML = """version: 1
accounts:
  - id: cybesis
    address: cybesis@gmail.com
    default: true
    aliases:
      - { address: support@cybesis.com, notify: true }
      - { address: billing@cybesis.com, notify: true }
      - { address: spam@cybesis.com, notify: false }
      - { address: sergey@cybesis.com }
  - id: other
    address: other@gmail.com
    aliases:
      - { address: x@other.com, notify: true }
"""


class TestNotifyAliasesFromAccounts:
    def _space(self, tmp_path) -> MailroomPaths:
        root = tmp_path / "space"
        (root / ".navig").mkdir(parents=True)
        (root / "mailroom").mkdir()
        (root / "mailroom" / "accounts.yaml").write_text(ACCOUNTS_YAML, encoding="utf-8")
        return MailroomPaths(root)

    def test_only_notify_true_aliases_of_the_default_account(self, tmp_path):
        paths = self._space(tmp_path)
        assert E.notify_aliases(paths) == ["support", "billing"]
        assert E.default_account(E.read_accounts(paths))["id"] == "cybesis"

    def test_deploy_vars_carry_the_list_and_the_forward_target(self, tmp_path):
        paths = self._space(tmp_path)
        assert E.deploy_vars(paths) == {
            "NOTIFY_ALIASES": "support,billing",
            "FORWARD_TO": "cybesis@gmail.com",
        }
        assert E.deploy_vars(None) == {}

    def test_missing_or_broken_accounts_yaml_yields_no_overrides(self, tmp_path):
        bare = tmp_path / "bare"
        (bare / ".navig").mkdir(parents=True)
        assert E.deploy_vars(MailroomPaths(bare)) == {}
        (bare / "mailroom").mkdir()
        (bare / "mailroom" / "accounts.yaml").write_text("not: [a mapping\n", encoding="utf-8")
        assert E.notify_aliases(MailroomPaths(bare)) == []

    def test_deploy_passes_the_vars_to_wrangler(self, tmp_path, monkeypatch):
        paths = self._space(tmp_path)
        seen: dict = {}

        class _R:
            returncode = 0
            stdout = b"Deployed"
            stderr = b""

        def fake_run(argv, **kw):
            seen["argv"] = argv
            return _R()

        monkeypatch.setattr(E.subprocess, "run", fake_run)
        code, out, overrides = E.deploy(paths=paths)
        assert code == 0 and "Deployed" in out
        assert overrides["NOTIFY_ALIASES"] == "support,billing"
        assert "--var" in seen["argv"] and "NOTIFY_ALIASES:support,billing" in seen["argv"]
        assert "--dry-run" not in seen["argv"]


class TestTokenScopes:
    def test_reads_prefer_the_read_only_token(self, tmp_path, monkeypatch):
        root = tmp_path / "space"
        (root / ".navig").mkdir(parents=True)
        (root / ".navig" / "config.yaml").write_text(
            f"mailroom:\n  edge:\n    url: {URL}\n", encoding="utf-8"
        )
        paths = MailroomPaths(root)
        store = {E.TOKEN_LABEL: "FULL", E.READ_TOKEN_LABEL: "READ"}
        monkeypatch.setattr(E, "_vault_secret", lambda label: store.get(label, ""))
        seen: list = []

        def fake_get(url, params=None, headers=None, timeout=None):
            seen.append(headers["Authorization"])
            return _Resp(200, {"days": 7, "total": 0, "by_alias": {}, "by_tag": {}, "top_domains": []})

        def fake_post(url, json=None, headers=None, timeout=None):
            seen.append(headers["Authorization"])
            return _Resp(200, {"ok": True, "result": {}})

        monkeypatch.setattr(E.httpx, "get", fake_get)
        monkeypatch.setattr(E.httpx, "post", fake_post)
        E.stats(paths, days=7)
        E.send(paths, to="a@b.c", subject="s", text="t")
        assert seen == ["Bearer READ", "Bearer FULL"]

        # No read token → reads fall back to the full one rather than failing.
        store.pop(E.READ_TOKEN_LABEL)
        seen.clear()
        E.stats(paths, days=1)
        assert seen == ["Bearer FULL"]

        # No token at all → a clear error that names the fix.
        store.clear()
        with pytest.raises(E.EdgeNotConfigured) as exc:
            E.stats(paths, days=1)
        assert "edge secrets" in str(exc.value)
