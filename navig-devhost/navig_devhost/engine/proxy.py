"""TLS-terminating transparent relay — trusted HTTPS in front of a plain-HTTP dev server.

Each site terminates TLS on its dedicated loopback:443 (with its mkcert cert) and
then pipes bytes verbatim to the dev server. Because it relays raw bytes after the
handshake — not parsed HTTP — every HTTP/1.1 feature works untouched: keep-alive,
chunked responses, SSE, and WebSocket upgrades (so Vite/Next HMR "just works").
"""

from __future__ import annotations

import socket
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass
class Site:
    domain: str
    ip: str
    https_port: int
    target_host: str
    target_port: int
    certfile: str
    keyfile: str


class Relay:
    """Runs one or more Sites until stopped (Ctrl+C or .stop())."""

    def __init__(self, sites: list[Site], on_log: Callable[[str], None] | None = None):
        self.sites = sites
        self.on_log = on_log or (lambda _msg: None)
        self._stop = threading.Event()
        self._listeners: list[socket.socket] = []

    # ── lifecycle ───────────────────────────────────────────────────────────
    def start(self) -> list[str]:
        """Bind + start accept loops. Returns human-readable bind errors (empty = all ok)."""
        errors: list[str] = []
        for site in self.sites:
            try:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(site.certfile, site.keyfile)
            except (ssl.SSLError, OSError) as exc:
                errors.append(f"{site.domain}: bad cert/key ({exc})")
                continue
            lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                lsock.bind((site.ip, site.https_port))
                lsock.listen(128)
            except OSError as exc:
                lsock.close()
                errors.append(f"{site.domain}: cannot bind {site.ip}:{site.https_port} ({exc})")
                continue
            self._listeners.append(lsock)
            threading.Thread(target=self._accept_loop, args=(lsock, ctx, site), daemon=True).start()
            self.on_log(f"🔒 https://{site.domain}  →  http://{site.target_host}:{site.target_port}")
        return errors

    def serve_forever(self) -> None:
        try:
            while not self._stop.is_set():
                time.sleep(0.4)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        for lsock in self._listeners:
            try:
                lsock.close()
            except OSError:
                pass
        self._listeners.clear()

    # ── connection handling ─────────────────────────────────────────────────
    def _accept_loop(self, lsock: socket.socket, ctx: ssl.SSLContext, site: Site) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = lsock.accept()
            except OSError:
                break  # listener closed on stop()
            threading.Thread(target=self._handle, args=(conn, ctx, site), daemon=True).start()

    def _handle(self, raw: socket.socket, ctx: ssl.SSLContext, site: Site) -> None:
        try:
            tls = ctx.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            _safe_close(raw)
            return
        try:
            upstream = socket.create_connection((site.target_host, site.target_port), timeout=5)
        except OSError:
            _send_502(tls, site)
            _safe_close(tls)
            return
        _pipe(tls, upstream, self._stop)


# ── module helpers ──────────────────────────────────────────────────────────
_PUMP_POLL = 30.0  # recv() timeout: bounds the block so pumps can check stop / the idle deadline
_PUMP_IDLE_MAX = 900.0  # close a connection idle this long — reaps a half-open peer that vanished


def _pipe(a: socket.socket, b: socket.socket, stop: "threading.Event | None" = None) -> None:
    def pump(src: socket.socket, dst: socket.socket) -> None:
        # Without a timeout, recv() blocks FOREVER when a peer vanishes without a FIN (a
        # dropped/killed client, NAT timeout) — so the pump threads never exit and the join()
        # below hangs, leaking 3 threads + 2 sockets per dead connection over a dev session.
        src.settimeout(_PUMP_POLL)
        idle = 0.0
        try:
            while True:
                try:
                    data = src.recv(65536)
                except socket.timeout:
                    idle += _PUMP_POLL
                    if (stop is not None and stop.is_set()) or idle >= _PUMP_IDLE_MAX:
                        break  # shutting down, or the peer is dead — stop waiting
                    continue  # idle but alive (data still flowing the other way / keepalives)
                except OSError:
                    break
                if not data:
                    break
                idle = 0.0
                try:
                    dst.sendall(data)
                except OSError:
                    break
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    t1 = threading.Thread(target=pump, args=(a, b), daemon=True)
    t2 = threading.Thread(target=pump, args=(b, a), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    _safe_close(a)
    _safe_close(b)


def _send_502(tls: socket.socket, site: Site) -> None:
    body = (
        f"devhost: dev server not reachable at {site.target_host}:{site.target_port}.\r\n"
        f"Start it (e.g. your dev command), then reload {site.domain}.\r\n"
    ).encode("utf-8")
    resp = (
        b"HTTP/1.1 502 Bad Gateway\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + body
    )
    try:
        tls.sendall(resp)
    except OSError:
        pass


def _safe_close(sock: socket.socket) -> None:
    try:
        sock.close()
    except OSError:
        pass
