"""A sync obs-websocket v5 client (protocol, not a third-party wrapper).

Protocol reference: obs-websocket 5.x — op 0 Hello · op 1 Identify ·
op 2 Identified · op 3 KeepAlive · op 4 KeepaliveAck · op 5 Event ·
op 6 Request · op 7 RequestResponse.

Design notes:
- One connection per CLI invocation (connect → identified → requests → close).
- ``eventSubscriptions = 0``: this client only ever issues requests.
- A keepalive thread honors the server's ``keepaliveInterval``; sends are
  serialized with a lock (the sync connection allows one concurrent sender,
  and we have two call sites).
- Auth per spec: ``secret = b64(sha256(password + salt))``,
  ``authentication = b64(sha256(secret + challenge))`` from the Hello payload.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import uuid
from typing import Any

try:
    from websockets.sync.client import connect as ws_connect
except ImportError as _exc:  # pragma: no cover — dependency guard
    ws_connect = None  # type: ignore[assignment]
    _IMPORT_ERROR: Exception | None = _exc
else:
    _IMPORT_ERROR = None

from navig_obs.engine.errors import ObsAuthError, ObsConnectionError, ObsRequestError

_RPC_VERSION = 1

# obs-websocket EventSubscription bits (v5) — used by `navig obs events`.
EVENT_TOPICS: dict[str, int] = {
    "general": 1 << 0,
    "config": 1 << 1,
    "scenes": 1 << 2,
    "inputs": 1 << 3,
    "transitions": 1 << 4,
    "outputs": 1 << 5,
    "sceneitems": 1 << 6,
    "media": 1 << 7,
    "vendors": 1 << 8,
    "ui": 1 << 9,
}
EVENT_ALL = 0xFFFFFFFF
EVENT_SCENES = EVENT_TOPICS["scenes"]


def compute_authentication(password: str, salt: str, challenge: str) -> str:
    """obs-websocket v5 challenge/response (see module docstring)."""
    secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest()).decode("ascii")
    return base64.b64encode(hashlib.sha256((secret + challenge).encode("utf-8")).digest()).decode("ascii")


class ObsClient:
    """Synchronous obs-websocket v5 client. Use as a context manager."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4455,
        password: str | None = None,
        *,
        connect_timeout: float = 5.0,
        request_timeout: float = 15.0,
    ) -> None:
        if _IMPORT_ERROR is not None:  # pragma: no cover
            raise ObsConnectionError(
                "the 'websockets' package is not installed (pip install 'websockets>=12.0')"
            ) from _IMPORT_ERROR
        self._host = host
        self._port = port
        self._password = password
        self._connect_timeout = connect_timeout
        self._request_timeout = request_timeout
        self._ws: Any = None
        self._send_lock = threading.Lock()
        self._keepalive_stop = threading.Event()
        self._keepalive_thread: threading.Thread | None = None
        self.identified: dict[str, Any] = {}
        self.hello: dict[str, Any] = {}

    # ── lifecycle ────────────────────────────────────────────────────────────
    def connect(self, event_subscriptions: int = 0) -> dict[str, Any]:
        """Open the socket, complete Hello/Identify, return the Identified payload.

        ``event_subscriptions`` is the obs-websocket v5 bitmask (0 = none, the
        default for request-only use; use :data:`EVENT_ALL` for `events` streaming).
        """
        if self._ws is not None:
            return self.identified
        url = f"ws://{self._host}:{self._port}"
        try:
            self._ws = ws_connect(url, open_timeout=self._connect_timeout, close_timeout=2.0)
        except Exception as exc:  # noqa: BLE001 — normalize every connect failure
            raise ObsConnectionError(
                f"cannot reach OBS websocket at {self._host}:{self._port} ({exc})"
            ) from exc

        auth_info: dict[str, Any] | None = None
        try:
            hello = self._recv_message(timeout=self._connect_timeout, purpose="Hello")
            if not isinstance(hello, dict) or hello.get("op") != 0:
                raise ObsConnectionError(f"expected obs-websocket Hello (op 0), got {hello!r}")
            self.hello = hello.get("d") or {}

            identify: dict[str, Any] = {
                "rpcVersion": _RPC_VERSION,
                "eventSubscriptions": event_subscriptions,
            }
            auth_info = self.hello.get("authentication")
            if auth_info:
                if not self._password:
                    raise ObsAuthError(
                        "OBS requires a websocket password — set NAVIG_OBS_PASSWORD "
                        "(or obs.password in navig config)"
                    )
                identify["authentication"] = compute_authentication(
                    self._password, auth_info.get("salt", ""), auth_info.get("challenge", "")
                )
            self._send({"op": 1, "d": identify})

            while True:
                msg = self._recv_message(timeout=self._connect_timeout, purpose="Identified")
                if not isinstance(msg, dict):
                    continue
                if msg.get("op") == 2:
                    self.identified = msg.get("d") or {}
                    break
                # Ignore stray events/acks during the handshake.
            self._start_keepalive()
            return self.identified
        except ObsAuthError:
            self._abort()
            raise
        except ObsConnectionError:
            # A close right after we sent Identify carrying auth = rejected handshake.
            self._abort()
            if auth_info:
                raise ObsAuthError(
                    f"OBS at {self._host}:{self._port} rejected the websocket password"
                ) from None
            raise
        except Exception as exc:  # noqa: BLE001
            self._abort()
            raise ObsConnectionError(f"handshake with OBS failed ({exc})") from exc

    def close(self) -> None:
        self._keepalive_stop.set()
        if self._keepalive_thread is not None:
            self._keepalive_thread.join(timeout=1.0)
            self._keepalive_thread = None
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001 — closing must never mask the real error
                pass
            self._ws = None

    def __enter__(self) -> ObsClient:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ── requests ─────────────────────────────────────────────────────────────
    def request(self, request_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Issue one request; return ``responseData`` ({} when absent).

        Raises ObsConnectionError (socket/timeout), ObsRequestError (rejected),
        or ObsAuthError (handshake) — never a bare Exception from the protocol.
        """
        if self._ws is None:
            self.connect()
        request_id = uuid.uuid4().hex
        payload: dict[str, Any] = {"requestType": request_type, "requestId": request_id}
        if data:
            payload["requestData"] = data
        self._send({"op": 6, "d": payload})

        deadline = time.monotonic() + self._request_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ObsConnectionError(
                    f"timed out after {self._request_timeout:.0f}s waiting for {request_type}"
                )
            msg = self._recv_message(timeout=remaining, purpose=request_type)
            if not isinstance(msg, dict):
                continue
            if msg.get("op") != 7:
                continue  # keepalive ack / event — not our response
            d = msg.get("d") or {}
            if d.get("requestId") != request_id:
                continue  # response to a previous request; serial use makes this rare
            status = d.get("requestStatus") or {}
            if status.get("result"):
                return d.get("responseData") or {}
            raise ObsRequestError(request_type, status.get("code"), status.get("comment"))

    def stream_events(self, deadline: float | None = None) -> dict[str, Any] | None:
        """Return the next event; None once ``deadline`` (time.monotonic) passes.

        Parses op-5 frames into ``{"eventType", "eventIntent", "eventData"}``;
        skips keepalive acks. Raises ObsConnectionError when the socket dies.
        A recv *timeout* is retried unless the deadline has passed — keepalive
        acks normally give us steady traffic, which is exactly why the deadline
        must be re-checked on every loop (an ack must never postpone it).
        """
        if self._ws is None:
            self.connect(event_subscriptions=EVENT_ALL)
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                return None
            timeout = 30.0
            if deadline is not None:
                timeout = max(min(30.0, deadline - time.monotonic()), 0.05)
            try:
                msg = self._recv_message(timeout=timeout, purpose="events")
            except ObsConnectionError as exc:
                if isinstance(exc.__cause__, TimeoutError):
                    if deadline is not None and time.monotonic() >= deadline:
                        return None
                    continue  # idle, not dead — keep waiting for the next event
                raise
            if isinstance(msg, dict) and msg.get("op") == 5:
                d = msg.get("d") or {}
                return {
                    "eventType": d.get("eventType"),
                    "eventIntent": d.get("eventIntent"),
                    "eventData": d.get("eventData") or {},
                }
            # op 4 keepalive ack — loop back (the deadline is re-checked above).

    # ── internals ────────────────────────────────────────────────────────────
    def _send(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            raise ObsConnectionError("not connected")
        with self._send_lock:
            try:
                self._ws.send(json.dumps(payload))
            except Exception as exc:  # noqa: BLE001 — includes ConnectionClosed
                raise ObsConnectionError(f"connection to OBS lost while sending ({exc})") from exc

    def _recv_message(self, *, timeout: float, purpose: str) -> Any:
        if self._ws is None:
            raise ObsConnectionError("not connected")
        try:
            raw = self._ws.recv(timeout=timeout)
        except TimeoutError as exc:
            raise ObsConnectionError(f"timed out waiting for {purpose} from OBS") from exc
        except Exception as exc:  # noqa: BLE001 — ConnectionClosed and friends
            raise ObsConnectionError(f"connection to OBS lost while waiting for {purpose} ({exc})") from exc
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ObsConnectionError(f"non-JSON frame from OBS while waiting for {purpose}") from exc

    def _abort(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001
            self._ws = None

    def _start_keepalive(self) -> None:
        interval_ms = self.hello.get("keepaliveInterval")
        if not interval_ms:
            return
        period = max(float(interval_ms) / 1000.0 / 2.0, 0.05)
        self._keepalive_stop.clear()

        def _loop() -> None:
            while not self._keepalive_stop.wait(period):
                try:
                    self._send({"op": 3, "d": {}})
                except ObsConnectionError:
                    return  # socket is gone; the request path will surface it

        self._keepalive_thread = threading.Thread(target=_loop, name="navig-obs-keepalive", daemon=True)
        self._keepalive_thread.start()
