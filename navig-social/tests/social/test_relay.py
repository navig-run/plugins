"""navig social relay — the operator's console for the navig-relay Worker.

A real stdlib HTTP server stands in for the Worker, implementing its contract
(auth, allowlist, idempotency, cancel-only-pending). Every request here is a genuine
HTTP round trip; nothing touches the network or a real relay.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from typer.testing import CliRunner

from navig_social import relay

TOKEN = "test-relay-token-7f3a"
ALLOWED = ["@miztizm", "@somaleto", "@somaneo", "@snsmnk", "@sch8ma", "@grabnite", "@navigrun", "@cybesis"]


# --------------------------------------------------------------------------- fake Worker

class FakeRelay:
    def __init__(self) -> None:
        self.posts: dict[str, dict] = {}
        self.requests: list[tuple[str, str, dict]] = []  # (method, path, headers)
        self.drains = 0


def _handler(state: FakeRelay):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep test output quiet
            pass

        def _send(self, code: int, body: dict) -> None:
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _authed(self) -> bool:
            return self.headers.get("authorization") == f"Bearer {TOKEN}"

        def _body(self) -> dict:
            n = int(self.headers.get("content-length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _route(self, method: str) -> None:
            u = urlparse(self.path)
            # HTTP header names are case-insensitive; record them lowercased so tests can't
            # depend on how urllib happens to capitalise them.
            state.requests.append((method, u.path, {k.lower(): v for k, v in self.headers.items()}))
            if u.path == "/health":
                return self._send(200, {"ok": True, "allowed_channels": ALLOWED})
            if not self._authed():
                return self._send(401, {"error": "unauthorized"})
            if method == "POST" and u.path == "/posts":
                b = self._body()
                if b.get("channel") not in ALLOWED:
                    return self._send(400, {"error": "channel not allowed", "channel": b.get("channel")})
                key = b.get("idempotency_key")
                for p in state.posts.values():
                    if key and p.get("idempotency_key") == key:
                        return self._send(200, {"deduplicated": True, "id": p["id"], "status": p["status"]})
                pid = str(uuid.uuid4())
                state.posts[pid] = {"id": pid, "status": "pending", **b, "preview": b["body"][:120]}
                return self._send(201, {"id": pid, "channel": b["channel"],
                                        "scheduled_at": b["scheduled_at"], "status": "pending"})
            if method == "GET" and u.path == "/posts":
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                rows = [p for p in state.posts.values()
                        if ("status" not in q or p["status"] == q["status"])
                        and ("channel" not in q or p["channel"] == q["channel"])]
                return self._send(200, {"count": len(rows), "posts": rows})
            if method == "DELETE" and u.path.startswith("/posts/"):
                pid = u.path.split("/")[-1]
                p = state.posts.get(pid)
                if not p or p["status"] != "pending":
                    return self._send(409, {"error": "not found, or no longer pending"})
                p["status"] = "cancelled"
                return self._send(200, {"id": pid, "status": "cancelled"})
            if method == "POST" and u.path == "/drain":
                # Read the body like a real server. Leaving it unread makes Windows answer
                # the close with RST, which is exactly how this suite once flaked.
                self._body()
                state.drains += 1
                due = [p for p in state.posts.values() if p["status"] == "pending"]
                for p in due:
                    p["status"] = "sent"
                return self._send(200, {"claimed": len(due), "sent": len(due), "failed": 0,
                                        "skipped": 0, "stuck": 0})
            return self._send(404, {"error": "not found"})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_DELETE(self):
            self._route("DELETE")

    return H


@pytest.fixture
def fake():
    state = FakeRelay()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def env(monkeypatch, fake):
    """Configure the relay through the environment only — the vault must not be consulted."""
    monkeypatch.setenv("NAVIG_RELAY_URL", fake.url)
    monkeypatch.setenv("NAVIG_RELAY_TOKEN", TOKEN)
    monkeypatch.setattr(relay, "_vault", lambda field: None)
    return fake


@pytest.fixture
def cli():
    from navig_social.commands.social import social_app

    runner = CliRunner()
    return lambda *args: runner.invoke(social_app, ["relay", *args])


def client_for(fake) -> relay.RelayClient:
    return relay.RelayClient(relay.RelayConfig(url=fake.url, token=TOKEN))


# --------------------------------------------------------------------------- parse_when

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def test_parse_when_now_and_relative():
    assert relay.parse_when("now", now=NOW) == int(NOW.timestamp())
    assert relay.parse_when("+30m", now=NOW) == int((NOW + timedelta(minutes=30)).timestamp())
    assert relay.parse_when("+2h", now=NOW) == int((NOW + timedelta(hours=2)).timestamp())
    assert relay.parse_when("+3D", now=NOW) == int((NOW + timedelta(days=3)).timestamp())
    assert relay.parse_when("+1w", now=NOW) == int((NOW + timedelta(weeks=1)).timestamp())


def test_parse_when_iso_with_offset_is_exact():
    assert relay.parse_when("2026-10-01T09:00:00Z") == int(datetime(2026, 10, 1, 9, tzinfo=timezone.utc).timestamp())
    assert relay.parse_when("2026-10-01T09:00+02:00") == int(datetime(2026, 10, 1, 7, tzinfo=timezone.utc).timestamp())


def test_parse_when_naive_iso_means_local_wall_clock():
    """'09:00' is what the operator reads on their own clock — not 09:00 UTC."""
    expected = int(datetime(2026, 10, 1, 9, 0).astimezone().timestamp())
    assert relay.parse_when("2026-10-01T09:00") == expected


@pytest.mark.parametrize("bad", ["", "tomorrow", "+2x", "32/13/2026", "+-3h"])
def test_parse_when_rejects_what_it_cannot_read(bad):
    with pytest.raises(ValueError):
        relay.parse_when(bad, now=NOW)


# --------------------------------------------------------------------------- shaping

@pytest.mark.parametrize("raw", ["somaleto", "@somaleto", " @somaleto ", "t.me/somaleto",
                                 "https://t.me/somaleto/", "HTTPS://www.T.me/somaleto"])
def test_normalize_channel(raw):
    assert relay.normalize_channel(raw) == "@somaleto"


def test_normalize_channel_rejects_empty():
    with pytest.raises(ValueError):
        relay.normalize_channel("  t.me/  ")


def test_idempotency_key_is_stable_and_discriminating():
    k = relay.idempotency_key("@miztizm", 1790000000, "hello")
    assert k == relay.idempotency_key("@miztizm", 1790000000, "hello")
    assert k != relay.idempotency_key("@somaleto", 1790000000, "hello")
    assert k != relay.idempotency_key("@miztizm", 1790000001, "hello")
    assert k != relay.idempotency_key("@miztizm", 1790000000, "hello!")
    assert k.startswith("cli-") and len(k) == 36


# --------------------------------------------------------------------------- configuration

def test_config_prefers_flag_then_env_then_vault(monkeypatch):
    monkeypatch.setattr(relay, "_vault", lambda f: {"url": "https://vault.example", "token": "vt"}[f])
    monkeypatch.delenv("NAVIG_RELAY_URL", raising=False)
    monkeypatch.delenv("NAVIG_RELAY_TOKEN", raising=False)
    assert relay.resolve_config() == relay.RelayConfig("https://vault.example", "vt")

    monkeypatch.setenv("NAVIG_RELAY_URL", "https://env.example/")
    monkeypatch.setenv("NAVIG_RELAY_TOKEN", "et")
    assert relay.resolve_config() == relay.RelayConfig("https://env.example", "et")  # trailing / stripped

    assert relay.resolve_config("https://flag.example").url == "https://flag.example"


# --------------------------------------------------------------------------- client round trips

def test_enqueue_sends_bearer_and_the_contract_fields(fake):
    out = client_for(fake).enqueue(channel="@somaleto", body="hi", scheduled_at=1790000000,
                                   parse_mode="HTML", disable_preview=True, key="k1")
    assert out["status"] == "pending"
    method, path, headers = fake.requests[-1]
    assert (method, path) == ("POST", "/posts")
    assert headers["authorization"] == f"Bearer {TOKEN}"
    stored = fake.posts[out["id"]]
    assert stored["parse_mode"] == "HTML" and stored["disable_preview"] is True
    assert stored["idempotency_key"] == "k1"


def test_health_does_not_send_the_token(fake):
    client_for(fake).health()
    _, _, headers = fake.requests[-1]
    assert "authorization" not in headers


def test_a_refusal_surfaces_the_relays_reason_and_status(fake):
    with pytest.raises(relay.RelayError) as err:
        client_for(fake).enqueue(channel="@notmine", body="x", scheduled_at=1790000000)
    assert err.value.status == 400
    assert "channel not allowed" in str(err.value)


def test_a_wrong_token_is_401_and_never_echoed(fake):
    bad = relay.RelayClient(relay.RelayConfig(url=fake.url, token="super-secret-wrong"))
    with pytest.raises(relay.RelayError) as err:
        bad.list()
    assert err.value.status == 401
    assert "super-secret-wrong" not in str(err.value)


def test_a_dropped_connection_is_a_clean_error_not_a_traceback():
    """Regression: urllib only wraps connection *setup* errors in URLError.

    A server that accepts and then hangs up raises a bare RemoteDisconnected /
    ConnectionResetError while the response is read. That escaped the client and
    crashed the CLI with a traceback (~1 in 75 calls on Windows, as a flaky test).
    """
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]

    def hang_up():
        for _ in range(3):
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            conn.recv(65536)
            conn.close()  # no response at all

    threading.Thread(target=hang_up, daemon=True).start()
    client = relay.RelayClient(relay.RelayConfig(url=f"http://127.0.0.1:{port}", token=TOKEN), timeout=5)
    try:
        for call in (client.health, client.drain,
                     lambda: client.enqueue(channel="@miztizm", body="x", scheduled_at=1)):
            with pytest.raises(relay.RelayError) as err:
                call()
            assert "dropped" in str(err.value)
            assert TOKEN not in str(err.value)
    finally:
        listener.close()


def test_unreachable_relay_is_a_clean_error():
    dead = relay.RelayClient(relay.RelayConfig(url="http://127.0.0.1:9", token=TOKEN), timeout=2)
    with pytest.raises(relay.RelayError) as err:
        dead.health()
    assert err.value.status == 0
    assert "unreachable" in str(err.value)


# --------------------------------------------------------------------------- CLI

def test_post_with_a_fixed_time_is_idempotent(cli, env):
    """A retried or re-run command must not queue a second broadcast."""
    outputs = [cli("post", "@miztizm", "-t", "navig is out", "--at", "2031-01-01T09:00:00Z")
               for _ in range(3)]
    assert all(r.exit_code == 0 for r in outputs), [r.output for r in outputs]
    assert len(env.posts) == 1
    assert "already queued" in outputs[-1].output


@pytest.mark.parametrize("at", ["now", "+2h", "+3d"])
def test_relative_time_run_twice_queues_once(cli, env, at):
    """Regression — found live: two identical `post … --at now` runs delivered TWO
    messages to @cybesis. The key hashed the resolved second, which moves every run."""
    first = cli("post", "cybesis", "-t", "same text", "--at", at)
    time.sleep(1.1)  # guarantee the resolved time differs, as it did live
    second = cli("post", "cybesis", "-t", "same text", "--at", at)
    assert first.exit_code == 0 and second.exit_code == 0, (first.output, second.output)
    assert len(env.posts) == 1
    assert "already queued" in second.output


def test_requeuing_a_cancelled_post_says_so_and_points_at_force(cli, env):
    """Found live: re-posting something cancelled earlier today said 'already queued',
    though the earlier copy will never go out."""
    cli("post", "somaleto", "-t", "maybe", "--at", "+1d")
    (pid,) = env.posts
    env.posts[pid]["status"] = "cancelled"
    r = cli("post", "somaleto", "-t", "maybe", "--at", "+1d")
    assert r.exit_code == 0, r.output
    assert "cancelled earlier" in r.output and "--force" in r.output
    assert len(env.posts) == 1


def test_force_queues_a_deliberate_repeat(cli, env):
    for _ in range(2):
        r = cli("post", "miztizm", "-t", "again", "--at", "now", "--force")
        assert r.exit_code == 0, r.output
    assert len(env.posts) == 2


def test_same_text_at_two_exact_times_is_two_posts(cli, env):
    cli("post", "somaleto", "-t", "weekly", "--at", "2031-01-01T09:00:00Z")
    cli("post", "somaleto", "-t", "weekly", "--at", "2031-01-08T09:00:00Z")
    assert len(env.posts) == 2


def test_relative_key_is_per_day_and_per_text():
    now_like = relay.When(1790000000, relative=True)
    later = relay.When(1790003600, relative=True)
    k = relay.idempotency_key("@m", now_like, "t", today="2026-09-30")
    assert k == relay.idempotency_key("@m", later, "t", today="2026-09-30")  # resolved time ignored
    assert k != relay.idempotency_key("@m", now_like, "t", today="2026-10-01")  # next day: new post
    assert k != relay.idempotency_key("@m", now_like, "t2", today="2026-09-30")
    assert k != relay.idempotency_key("@m", relay.When(1790000000, relative=False), "t")


def test_relative_key_uses_the_local_calendar_day():
    """Caught by core's no-UTC-calendar-day guard: the day must be the operator's local
    day, or "once per day" rolls over at 02:00 in France instead of midnight."""
    from datetime import date

    w = relay.When(1790000000, relative=True)
    assert relay.idempotency_key("@m", w, "t") == relay.idempotency_key(
        "@m", w, "t", today=date.today().isoformat())


def test_post_reads_a_file(cli, env, tmp_path):
    note = tmp_path / "note.md"
    note.write_text("Жизнь диалектична.\n", encoding="utf-8")
    r = cli("post", "t.me/somaleto", "--file", str(note), "--at", "+1d")
    assert r.exit_code == 0, r.output
    (post,) = env.posts.values()
    assert post["channel"] == "@somaleto" and post["body"] == "Жизнь диалектична."


def test_dry_run_sends_nothing(cli, env):
    r = cli("post", "somaleto", "-t", "draft", "--at", "+1h", "--dry-run")
    assert r.exit_code == 0, r.output
    assert "would queue" in r.output
    assert env.requests == []


@pytest.mark.parametrize("args,needle", [
    (["somaleto"], "exactly one of --text or --file"),
    (["somaleto", "-t", "x", "-f", "y.md"], "exactly one of --text or --file"),
    (["somaleto", "-t", "   "], "empty"),
    (["somaleto", "-t", "x" * 4097], "4096"),
    (["somaleto", "-t", "x", "--at", "someday"], "can't read"),
    (["somaleto", "-t", "x", "--parse-mode", "markdown"], "HTML or MarkdownV2"),
])
def test_post_rejects_bad_input_before_any_request(cli, env, args, needle):
    r = cli("post", *args)
    assert r.exit_code == 1
    assert needle in r.output
    assert env.requests == []


def test_missing_url_names_how_to_set_it(cli, monkeypatch):
    monkeypatch.delenv("NAVIG_RELAY_URL", raising=False)
    monkeypatch.delenv("NAVIG_RELAY_TOKEN", raising=False)
    monkeypatch.setattr(relay, "_vault", lambda f: None)
    r = cli("queue")
    assert r.exit_code == 1
    assert "NAVIG_RELAY_URL" in r.output


def test_queue_lists_and_filters(cli, env):
    cli("post", "somaleto", "-t", "a", "--at", "2031-01-01T09:00:00Z")
    cli("post", "miztizm", "-t", "b", "--at", "2031-01-02T09:00:00Z")
    r = cli("queue", "--channel", "somaleto", "--json")
    assert r.exit_code == 0, r.output
    rows = json.loads(r.output)
    assert [p["channel"] for p in rows] == ["@somaleto"]


def test_cancel_by_unique_prefix(cli, env):
    cli("post", "somaleto", "-t", "cancel me", "--at", "2031-01-01T09:00:00Z")
    (pid,) = env.posts
    r = cli("cancel", pid[:8])
    assert r.exit_code == 0, r.output
    assert env.posts[pid]["status"] == "cancelled"


def test_cancel_refuses_an_ambiguous_prefix(cli, env):
    cli("post", "somaleto", "-t", "one", "--at", "2031-01-01T09:00:00Z")
    cli("post", "somaleto", "-t", "two", "--at", "2031-01-02T09:00:00Z")
    r = cli("cancel", "")  # empty prefix matches everything
    assert r.exit_code == 1
    assert "matches 2" in r.output
    assert all(p["status"] == "pending" for p in env.posts.values())


def test_drain_reports(cli, env):
    queued = cli("post", "somaleto", "-t", "now", "--at", "2031-01-01T09:00:00Z")
    assert queued.exit_code == 0, queued.output
    r = cli("drain")
    assert r.exit_code == 0, r.output
    assert "sent 1" in r.output
    assert env.drains == 1


def test_status_flags_stuck_posts(cli, env):
    cli("post", "somaleto", "-t", "x", "--at", "2031-01-01T09:00:00Z")
    next(iter(env.posts.values()))["status"] = "stuck"
    r = cli("status")
    assert r.exit_code == 0, r.output
    assert "stuck 1" in r.output
    assert "may or may not have landed" in r.output


# --------------------------------------------------------------------------- standalone

def test_relay_client_imports_without_navig(monkeypatch):
    """navig-social must work under `pip install navig-social` alone."""
    for mod in [m for m in sys.modules if m == "navig" or m.startswith("navig.")]:
        monkeypatch.setitem(sys.modules, mod, None)
    monkeypatch.setitem(sys.modules, "navig", None)
    import importlib

    mod = importlib.reload(relay)
    assert mod.normalize_channel("miztizm") == "@miztizm"
