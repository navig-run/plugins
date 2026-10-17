"""Vault federation client — a device asks the authority, one secret at a time.

Credentials (authority URL, device id, device key) and the optional offline
cache are ordinary items of THIS machine's own vault — so they are sealed under
the local VMK (DPAPI on Windows), backed up and rotated like everything else:

    federation/client           {"url", "device_id", "device_key", "name", "kind"}
    federation/cache/<label>    {"value", "version", "expires_at"}

Offline behaviour, stated plainly:

* a value is cached ONLY when its grant has ``cache_ttl_s > 0`` (default 0);
* a cached value is served only while the authority is UNREACHABLE and only
  until ``expires_at`` — never past it, never instead of a live answer;
* a 401 from the authority (device revoked / expired) wipes the whole cache, a
  403 wipes that label; :meth:`sync` drops every label the authority no longer
  allows. A device that never reconnects keeps a cached copy until its TTL runs
  out — revocation cannot reach a machine that is offline.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from . import protocol as P

__all__ = [
    "AuthorityUnavailable",
    "DeviceRevoked",
    "FederationClient",
    "FederationError",
    "HttpTransport",
]

CLIENT_LABEL = "federation/client"
CACHE_PREFIX = "federation/cache/"
TIMEOUT_S = 5.0


class FederationError(RuntimeError):
    """The authority refused or could not be used (messages never carry a value)."""


class AuthorityUnavailable(FederationError):
    """The authority could not be reached (network, outage) and nothing valid is cached."""


class DeviceRevoked(FederationError):
    """The authority no longer accepts this device (revoked, expired, unknown)."""


Transport = Callable[[str, str, bytes, dict], tuple[int, dict]]


class HttpTransport:
    """POST JSON to ``<base url><path>`` with the stdlib; raises AuthorityUnavailable on I/O."""

    def __init__(self, base_url: str, timeout: float = TIMEOUT_S) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def __call__(self, method: str, path: str, body: bytes, headers: dict) -> tuple[int, dict]:
        req = urllib.request.Request(
            self.base_url + path, data=body, method=method,
            headers={**headers, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 — operator-set URL
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read() or b"{}")
            except ValueError:
                payload = {}
            return exc.code, payload
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise AuthorityUnavailable(f"vault authority unreachable ({type(exc).__name__})") from exc


class FederationClient:
    def __init__(self, local_vault, *, transport: Transport | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.vault = local_vault
        self._transport = transport
        self._clock = clock

    # ── enrollment ───────────────────────────────────────────────────────────

    def credentials(self) -> dict | None:
        try:
            return json.loads(self.vault.get_bytes(CLIENT_LABEL))
        except KeyError:
            return None

    def enrolled(self) -> bool:
        return self.credentials() is not None

    def _send(self, path: str, payload: dict, *, signed: bool = True) -> tuple[int, dict]:
        creds = self.credentials()
        transport = self._transport
        if transport is None:
            if not creds:
                raise FederationError("this machine is not enrolled with a vault authority")
            transport = HttpTransport(creds["url"])
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers: dict = {}
        if signed:
            if not creds:
                raise FederationError("this machine is not enrolled with a vault authority")
            headers = P.sign(creds["device_id"], base64.b64decode(creds["device_key"]), "POST", path, body,
                             now=self._clock())
        status, data = transport("POST", path, body, headers)
        if status == 429:
            # Not a revocation and not an outage: keep the cache, say when to retry.
            raise FederationError(
                f"the vault authority is rate-limiting this device — retry in {data.get('retry_after', 60)}s"
            )
        if status == 401 and data.get("error") == "clock_skew":
            drift = int(self._clock() - float(data.get("time", self._clock())))
            raise FederationError(
                f"this machine's clock is {drift:+d}s off the vault authority's "
                f"(limit ±{P.SKEW_S}s) — fix the time sync; nothing was revoked"
            )
        data["_nonce"] = headers.get(P.H_NONCE, "")
        return status, data

    def join(self, url: str, code: str) -> dict:
        """Redeem an enrollment code; store the device credentials in the local vault."""
        if self.enrolled():
            raise FederationError("already enrolled — `navig vault leave` first")
        if self._transport is None:
            self._transport = HttpTransport(url)
        status, data = self._send("/api/vault/v1/enroll", {"code": code}, signed=False)
        if status != 200:
            raise FederationError(f"enrollment refused ({data.get('error', status)})")
        creds = {
            "url": url, "device_id": data["device_id"], "device_key": data["device_key"],
            "name": data.get("name", ""), "kind": data.get("kind", "client"),
        }
        self.vault.put(CLIENT_LABEL, json.dumps(creds).encode("utf-8"), metadata={"federation": "client"})
        return {k: creds[k] for k in ("url", "device_id", "name", "kind")}

    def leave(self) -> int:
        """Forget the credentials and every cached value. Returns cached entries removed."""
        n = self.purge_cache()
        self.vault.delete(CLIENT_LABEL)
        return n

    # ── resolving ────────────────────────────────────────────────────────────

    def resolve(self, ref: str) -> str:
        from ..refs import parse_ref  # noqa: PLC0415

        label = parse_ref(ref).label
        creds = self.credentials()
        if creds is None:
            raise FederationError("this machine is not enrolled with a vault authority")
        try:
            status, data = self._send("/api/vault/v1/resolve", {"ref": ref})
        except AuthorityUnavailable:
            cached = self._cached(label)
            if cached is not None:
                return cached
            raise
        if status == 200:
            key = base64.b64decode(creds["device_key"])
            if "sealed" in data:
                value = P.open_value(key, data["_nonce"], data["label"], data["version"], data["sealed"])
            else:
                value = data["value"]
            if data.get("cache_ttl_s", 0) > 0:
                self._store_cache(label, value, data["version"], data["cache_ttl_s"])
            else:
                self._drop_cache(label)
            return value
        if status == 401:
            self.purge_cache()
            raise DeviceRevoked("the vault authority no longer accepts this device")
        if status == 403:
            self._drop_cache(label)
            raise PermissionError(f"this device has no grant for {label!r}")
        if status == 404:
            raise KeyError(label)
        raise FederationError(f"vault authority error ({data.get('error', status)})")

    def status(self) -> dict:
        status, data = self._send("/api/vault/v1/status", {})
        if status == 401:
            self.purge_cache()
            raise DeviceRevoked("the vault authority no longer accepts this device")
        if status != 200:
            raise FederationError(f"vault authority error ({data.get('error', status)})")
        data.pop("_nonce", None)
        return data

    # ── cache ────────────────────────────────────────────────────────────────

    def _store_cache(self, label: str, value: str, version: int, ttl_s: int) -> None:
        entry = {"value": value, "version": version, "expires_at": self._clock() + ttl_s}
        self.vault.put(CACHE_PREFIX + label, json.dumps(entry).encode("utf-8"), metadata={"federation": "cache"})

    def _drop_cache(self, label: str) -> None:
        self.vault.delete(CACHE_PREFIX + label)

    def _cached(self, label: str) -> str | None:
        try:
            entry = json.loads(self.vault.get_bytes(CACHE_PREFIX + label))
        except KeyError:
            return None
        if entry.get("expires_at", 0) <= self._clock():
            self._drop_cache(label)
            return None
        return str(entry["value"])

    def cache_entries(self) -> list[dict]:
        """Cached labels with expiry — no values."""
        out = []
        for item in self.vault._store.list():
            if not item.label.startswith(CACHE_PREFIX):
                continue
            try:
                entry = json.loads(self.vault.get_bytes(item.label))
            except Exception:  # noqa: BLE001 — an unreadable entry is reported, then purged by sync
                entry = {}
            out.append({
                "label": item.label[len(CACHE_PREFIX):],
                "version": entry.get("version"),
                "expires_at": entry.get("expires_at"),
                "expired": entry.get("expires_at", 0) <= self._clock(),
            })
        return out

    def purge_cache(self) -> int:
        n = 0
        for item in self.vault._store.list():
            if item.label.startswith(CACHE_PREFIX):
                self.vault.delete(item.label)
                n += 1
        return n

    def sync(self) -> dict:
        """Drop expired entries; ask the authority which cached labels may stay."""
        entries = self.cache_entries()
        expired = [e["label"] for e in entries if e["expired"]]
        for label in expired:
            self._drop_cache(label)
        live = [e["label"] for e in entries if not e["expired"]]
        status, data = self._send("/api/vault/v1/sync", {"labels": live})
        if status == 401:
            n = self.purge_cache()
            raise DeviceRevoked(f"the vault authority no longer accepts this device ({n} cached value(s) wiped)")
        if status != 200:
            raise FederationError(f"vault authority error ({data.get('error', status)})")
        dropped = list(data.get("drop", []))
        versions = {e["label"]: e["version"] for e in entries}
        keep = data.get("keep", {})
        # A cached copy older than the authority's current version is stale: drop it,
        # the next online read fetches the new value.
        dropped += [lb for lb, info in keep.items() if versions.get(lb) != info.get("version")]
        for label in dropped:
            self._drop_cache(label)
        return {"expired": expired, "dropped": dropped, "kept": sorted(set(keep) - set(dropped))}
