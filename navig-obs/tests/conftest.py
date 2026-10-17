"""Fake obs-websocket v5 server for navig-obs tests (in-process, thread-per-conn).

Implements the real handshake (Hello → Identify → Identified), the v5 auth
challenge/response, keepalive acks, and request/response with canned handlers —
enough to test the client and every CLI verb without a running OBS.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading

import pytest

SALT = "navig-obs-test-salt"
CHALLENGE = "navig-obs-test-challenge"

# 1x1 transparent PNG for screenshot tests.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQAB"
    "h6FO1AAAAABJRU5ErkJggg=="
)


def expected_auth(password: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + SALT).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + CHALLENGE).encode()).digest()).decode()


class FakeObs:
    """A scripted obs-websocket server. ``requests`` maps requestType → response.

    A value may be: a dict (responseData), a callable(requestData) → dict, or an
    Exception instance/class (answered with result=false + str(exc)).
    """

    def __init__(self, password: str | None = None, requests: dict | None = None, events: list | None = None):
        self.password = password
        self.requests = requests or {}
        self.events = events or []
        self.received: list[tuple[str, dict | None]] = []
        self.subscriptions: int | None = None
        self.keepalives = 0
        self.port = 0
        self._server = None
        self._lock = threading.Lock()

    def start(self) -> "FakeObs":
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        from websockets.sync.server import serve

        # websockets>=14: serve() binds, __enter__ does NOT accept — the accept
        # loop is serve_forever(), which blocks, so it runs in a daemon thread.
        self._server = serve(self._handler, "127.0.0.1", self.port)
        self._server.__enter__()
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="fake-obs-accept", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:  # noqa: BLE001 — teardown must not mask test failures
                pass
            try:
                self._server.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            self._server = None
        if getattr(self, "_thread", None) is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # ── protocol ─────────────────────────────────────────────────────────────
    def _handler(self, ws) -> None:  # noqa: ANN001 — websockets sync server socket
        hello: dict = {
            "op": 0,
            "d": {
                "obsVersion": "32.2.2",
                "obsWebSocketVersion": "5.5.2",
                "rpcVersion": 1,
                "negotiatedRpcVersion": 1,
                "keepaliveInterval": 500,
            },
        }
        if self.password:
            hello["d"]["authentication"] = {"salt": SALT, "challenge": CHALLENGE}
        ws.send(json.dumps(hello))
        try:
            raw = ws.recv(timeout=5)
        except Exception:  # noqa: BLE001
            return
        msg = json.loads(raw)
        if msg.get("op") != 1:
            return
        identify = msg.get("d") or {}
        if self.password:
            if identify.get("authentication") != expected_auth(self.password):
                ws.close(4401, "authentication failed")
                return
        with self._lock:
            self.subscriptions = identify.get("eventSubscriptions")
        ws.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))
        for ev in self.events:
            ws.send(json.dumps({"op": 5, "d": ev}))
        while True:
            try:
                raw = ws.recv(timeout=10)
            except TimeoutError:
                continue
            except Exception:  # noqa: BLE001 — client closed
                return
            try:
                incoming = json.loads(raw)
            except json.JSONDecodeError:
                continue
            op = incoming.get("op")
            if op == 3:
                with self._lock:
                    self.keepalives += 1
                ws.send(json.dumps({"op": 4, "d": {}}))
            elif op == 6:
                self._answer(ws, incoming.get("d") or {})

    def _answer(self, ws, d: dict) -> None:
        rtype = d.get("requestType", "")
        rid = d.get("requestId", "")
        with self._lock:
            self.received.append((rtype, d.get("requestData")))
        handler = self.requests.get(rtype)
        status: dict
        response: dict = {}
        if handler is None:
            status = {"result": False, "code": 600, "comment": f"Unknown request type {rtype}"}
        elif isinstance(handler, type) and issubclass(handler, Exception):
            status = {"result": False, "code": 207, "comment": str(handler())}
        elif isinstance(handler, Exception):
            status = {"result": False, "code": 207, "comment": str(handler)}
        else:
            status = {"result": True, "code": 100}
            response = handler(d.get("requestData") or {}) if callable(handler) else dict(handler)
        ws.send(
            json.dumps(
                {
                    "op": 7,
                    "d": {
                        "requestType": rtype,
                        "requestId": rid,
                        "requestStatus": status,
                        **({"responseData": response} if status.get("result") else {}),
                    },
                }
            )
        )


@pytest.fixture
def obs_factory():
    """Make FakeObs servers; guarantees all are stopped after the test."""
    servers: list[FakeObs] = []

    def _make(**kwargs) -> FakeObs:
        server = FakeObs(**kwargs).start()
        servers.append(server)
        return server

    yield _make
    for server in servers:
        server.stop()


@pytest.fixture
def unused_port() -> int:
    """A port that was just free (for 'OBS not running' tests)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port
