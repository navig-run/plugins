"""Vault federation end to end: one authority, several devices, real signing + sealing.

The transport is in-process (``Authority.handle``), so every byte a device sends
and receives is what an HTTP hop would carry — the harness can record it, replay
it, drop the network, or move the clock.
"""

from __future__ import annotations

import json

import pytest

from navig_vault import keyslots
from navig_vault.core import Vault
from navig_vault.crypto import CryptoError
from navig_vault.federation import protocol as P
from navig_vault.federation.authority import Authority
from navig_vault.federation.client import (
    AuthorityUnavailable,
    DeviceRevoked,
    FederationClient,
    FederationError,
)
from navig_vault.session import SessionStore

SSH_PW = "vps-ssh-Pa55"
DB_PW = "o2-db-root"


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


class Net:
    """In-process transport: records traffic, can go down, can mark itself relayed."""

    def __init__(self, authority: Authority) -> None:
        self.authority = authority
        self.up = True
        self.relayed = False
        self.log: list[tuple[str, dict, bytes, int, dict]] = []

    def __call__(self, method, path, body, headers):
        if not self.up:
            raise AuthorityUnavailable("vault authority unreachable (ConnectionRefusedError)")
        status, data = self.authority.handle(method, path, headers, body, relayed=self.relayed)
        self.log.append((path, dict(headers), body, status, json.loads(json.dumps(data))))
        return status, dict(data)


@pytest.fixture(autouse=True)
def _clean():
    keyslots.forget_cached_vmks()
    SessionStore.clear()
    yield
    keyslots.forget_cached_vmks()
    SessionStore.clear()


@pytest.fixture
def world(tmp_path):
    clock = Clock()
    av = Vault(tmp_path / "outpost")
    av.put("host/cybesis-vps/ssh_password", json.dumps({"value": SSH_PW}).encode())
    av.put("host/o2switch/database.root_password", json.dumps({"value": DB_PW}).encode())
    authority = Authority(av, clock=clock)
    net = Net(authority)

    def device(name: str, kind: str = "client", device_ttl_s: int | None = None) -> FederationClient:
        local = Vault(tmp_path / name)
        fed = FederationClient(local, transport=net, clock=clock)
        fed.join("http://outpost.test", authority.create_enrollment(name, kind=kind, device_ttl_s=device_ttl_s))
        return fed

    yield clock, authority, net, device, av
    av._store.close()


def _id(fed: FederationClient) -> str:
    return fed.credentials()["device_id"]


# ── retrieval and scope ─────────────────────────────────────────────────────


def test_a_device_reads_a_granted_secret_and_the_wire_never_carries_it(world):
    clock, authority, net, device, _ = world
    win = device("neuron-pc")
    authority.grant(_id(win), "vault://hosts/cybesis-vps/ssh")

    assert win.resolve("vault://hosts/cybesis-vps/ssh") == SSH_PW

    path, headers, body, status, data = net.log[-1]
    assert status == 200 and "sealed" in data and "value" not in data
    wire = json.dumps(data) + json.dumps(headers) + body.decode()
    assert SSH_PW not in wire, "plaintext crossed the transport"
    # No credential on the wire either: a signature, never the device key.
    assert win.credentials()["device_key"] not in wire


def test_a_server_gets_only_what_it_was_granted_and_denials_are_audited(world):
    clock, authority, net, device, av = world
    vps = device("cybesis-vps", kind="server")
    authority.grant(_id(vps), "host/cybesis-vps/*")

    assert vps.resolve("vault:host/cybesis-vps/ssh_password") == SSH_PW
    with pytest.raises(PermissionError):
        vps.resolve("vault://hosts/o2switch/db-root")

    item = av._store.get("host/o2switch/database.root_password")
    rows = av._store.get_audit(item.id)
    denied = [r for r in rows if r["action"] == "denied"]
    assert denied and denied[0]["actor"] == f"device:{_id(vps)}"
    assert all(DB_PW not in (r["detail"] or "") for r in rows)


def test_no_grant_means_denied_everything(world):
    _, _, _, device, _ = world
    fresh = device("new-box")
    with pytest.raises(PermissionError):
        fresh.resolve("vault://hosts/cybesis-vps/ssh")


def test_no_pattern_can_hand_out_another_devices_key(world):
    _, authority, _, device, _ = world
    a, b = device("a"), device("b")
    with pytest.raises(ValueError, match="never be granted"):
        authority.grant(_id(a), "federation/*")
    authority.grant(_id(a), "*")
    with pytest.raises(PermissionError):
        a.resolve(f"vault:federation/device/{_id(b)}")


# ── offline cache, revocation, expiry ───────────────────────────────────────


def test_a_cached_secret_serves_offline_until_its_ttl_and_not_after(world):
    clock, authority, net, device, _ = world
    win = device("neuron-pc")
    authority.grant(_id(win), "host/cybesis-vps/*", cache_ttl_s=3600)
    assert win.resolve("vault://hosts/cybesis-vps/ssh") == SSH_PW

    net.up = False
    assert win.resolve("vault://hosts/cybesis-vps/ssh") == SSH_PW  # offline, cached
    clock.t += 3601
    with pytest.raises(AuthorityUnavailable):
        win.resolve("vault://hosts/cybesis-vps/ssh")
    assert win.cache_entries() == [], "an expired copy is deleted, not kept"


def test_without_a_cache_grant_an_outage_is_an_honest_error(world):
    _, authority, net, device, _ = world
    win = device("neuron-pc")
    authority.grant(_id(win), "host/cybesis-vps/*")  # cache_ttl_s = 0
    assert win.resolve("vault://hosts/cybesis-vps/ssh") == SSH_PW
    assert win.cache_entries() == []
    net.up = False
    with pytest.raises(AuthorityUnavailable):
        win.resolve("vault://hosts/cybesis-vps/ssh")


def test_revocation_is_immediate_online_and_wipes_the_cache(world):
    _, authority, _, device, _ = world
    win = device("neuron-pc")
    authority.grant(_id(win), "host/cybesis-vps/*", cache_ttl_s=3600)
    win.resolve("vault://hosts/cybesis-vps/ssh")
    assert win.cache_entries()

    authority.revoke_device(_id(win))

    with pytest.raises(DeviceRevoked):
        win.resolve("vault://hosts/cybesis-vps/ssh")
    assert win.cache_entries() == []


def test_revocation_cannot_reach_a_device_that_stays_offline(world):
    """The documented limit: a cached copy outlives revocation on an offline device until its TTL."""
    clock, authority, net, device, _ = world
    win = device("neuron-pc")
    authority.grant(_id(win), "host/cybesis-vps/*", cache_ttl_s=600)
    win.resolve("vault://hosts/cybesis-vps/ssh")
    authority.revoke_device(_id(win))
    net.up = False

    assert win.resolve("vault://hosts/cybesis-vps/ssh") == SSH_PW  # still served — the limit
    clock.t += 601
    with pytest.raises(AuthorityUnavailable):
        win.resolve("vault://hosts/cybesis-vps/ssh")


def test_device_credentials_expire(world):
    clock, authority, _, device, _ = world
    temp = device("contractor", device_ttl_s=60)
    authority.grant(_id(temp), "host/cybesis-vps/*")
    assert temp.resolve("vault://hosts/cybesis-vps/ssh") == SSH_PW
    clock.t += 61
    with pytest.raises(DeviceRevoked):
        temp.resolve("vault://hosts/cybesis-vps/ssh")


def test_grants_expire(world):
    clock, authority, _, device, _ = world
    win = device("neuron-pc")
    authority.grant(_id(win), "host/cybesis-vps/*", expires_in_s=30)
    assert win.resolve("vault://hosts/cybesis-vps/ssh") == SSH_PW
    clock.t += 31
    with pytest.raises(PermissionError):
        win.resolve("vault://hosts/cybesis-vps/ssh")


def test_sync_drops_what_is_no_longer_granted_and_stale_versions(world):
    _, authority, _, device, av = world
    win = device("neuron-pc")
    authority.grant(_id(win), "host/*", cache_ttl_s=3600)
    win.resolve("vault://hosts/cybesis-vps/ssh")
    win.resolve("vault://hosts/o2switch/db-root")

    authority.revoke_grants(_id(win), "host/*")
    authority.grant(_id(win), "host/cybesis-vps/*", cache_ttl_s=3600)
    av.put("host/cybesis-vps/ssh_password", json.dumps({"value": "rotated"}).encode())

    result = win.sync()

    assert "host/o2switch/database.root_password" in result["dropped"]  # grant gone
    assert "host/cybesis-vps/ssh_password" in result["dropped"]  # stale version
    assert win.cache_entries() == []
    assert win.resolve("vault://hosts/cybesis-vps/ssh") == "rotated"


# ── request integrity ───────────────────────────────────────────────────────


def test_a_replayed_request_is_refused(world):
    _, authority, net, device, _ = world
    win = device("neuron-pc")
    authority.grant(_id(win), "host/cybesis-vps/*")
    win.resolve("vault://hosts/cybesis-vps/ssh")
    path, headers, body, _, _ = net.log[-1]

    status, _ = authority.handle("POST", path, headers, body, relayed=False)
    assert status == 401


def test_a_skewed_clock_is_refused_but_not_mistaken_for_revocation(world):
    clock, authority, net, device, _ = world
    win = device("neuron-pc")
    authority.grant(_id(win), "host/cybesis-vps/*", cache_ttl_s=3600)
    win.resolve("vault://hosts/cybesis-vps/ssh")
    win._clock = lambda: clock.t + P.SKEW_S + 5

    with pytest.raises(FederationError, match="clock") as exc:
        win.resolve("vault://hosts/cybesis-vps/ssh")
    assert not isinstance(exc.value, DeviceRevoked)
    win._clock = clock
    assert win.cache_entries(), "a wrong clock must not wipe the cache"


def test_a_device_cannot_sign_as_another(world):
    _, authority, net, device, _ = world
    a, b = device("a"), device("b")
    authority.grant(_id(b), "host/cybesis-vps/*")
    import base64

    body = json.dumps({"ref": "vault:host/cybesis-vps/ssh_password"}).encode()
    forged = P.sign(_id(b), base64.b64decode(a.credentials()["device_key"]), "POST", "/api/vault/v1/resolve", body,
                    now=world[0].t)
    status, _ = authority.handle("POST", "/api/vault/v1/resolve", forged, body, relayed=False)
    assert status == 401


def test_a_tampered_sealed_value_does_not_open(world):
    _, authority, net, device, _ = world
    win = device("neuron-pc")
    authority.grant(_id(win), "host/*")
    win.resolve("vault://hosts/cybesis-vps/ssh")
    _, headers, _, _, data = net.log[-1]
    import base64

    key = base64.b64decode(win.credentials()["device_key"])
    with pytest.raises(CryptoError):  # moved to another label
        P.open_value(key, headers[P.H_NONCE], "host/o2switch/database.root_password", data["version"], data["sealed"])


def test_relayed_requests_cannot_enroll_or_ask_for_plaintext(world):
    _, authority, net, device, _ = world
    win = device("neuron-pc")
    authority.grant(_id(win), "host/*")
    code = authority.create_enrollment("relayed-box")
    status, data = authority.handle("POST", "/api/vault/v1/enroll", {}, json.dumps({"code": code}).encode(),
                                    relayed=True)
    assert status == 403 and data["error"] == "enroll_requires_direct_connection"

    import base64

    body = json.dumps({"ref": "vault:host/cybesis-vps/ssh_password"}).encode()
    h = P.sign(_id(win), base64.b64decode(win.credentials()["device_key"]), "POST", "/api/vault/v1/resolve", body,
               seal="none", now=world[0].t)
    status, data = authority.handle("POST", "/api/vault/v1/resolve", h, body, relayed=True)
    assert status == 400 and SSH_PW not in json.dumps(data)


def test_a_pending_device_can_be_granted_before_it_joins_but_cannot_authenticate(world, tmp_path):
    clock, authority, net, _, _ = world
    code = authority.create_enrollment("cybesis-vps", kind="server")
    pending = authority.find_device("cybesis-vps")
    assert authority._device_state(pending) == "pending"
    authority.grant(pending["id"], "host/cybesis-vps/*")  # enroll → grant → join

    import base64

    body = b'{"ref": "vault:host/cybesis-vps/ssh_password"}'
    forged = P.sign(pending["id"], base64.b64decode(base64.b64encode(b"k" * 32)), "POST",
                    "/api/vault/v1/resolve", body, now=clock.t)
    assert authority.handle("POST", "/api/vault/v1/resolve", forged, body, relayed=False)[0] == 401

    fed = FederationClient(Vault(tmp_path / "vps"), transport=net, clock=clock)
    fed.join("http://outpost.test", code)
    assert fed.credentials()["device_id"] == pending["id"]
    assert fed.resolve("vault://hosts/cybesis-vps/ssh") == SSH_PW
    with pytest.raises(ValueError, match="already enrolled"):
        authority.create_enrollment("cybesis-vps")


def test_an_enrollment_code_works_once_and_expires(world):
    clock, authority, net, _, _ = world
    code = authority.create_enrollment("box")
    body = json.dumps({"code": code}).encode()
    assert authority.handle("POST", "/api/vault/v1/enroll", {}, body, relayed=False)[0] == 200
    assert authority.handle("POST", "/api/vault/v1/enroll", {}, body, relayed=False)[0] == 403
    late = authority.create_enrollment("late-box")
    clock.t += 601
    assert authority.handle("POST", "/api/vault/v1/enroll", {}, json.dumps({"code": late}).encode(),
                            relayed=False)[0] == 403


# ── authority lifecycle ─────────────────────────────────────────────────────


def test_devices_keep_working_after_the_authority_rotates_its_master_key(world):
    _, authority, _, device, av = world
    win = device("neuron-pc")
    authority.grant(_id(win), "host/cybesis-vps/*")
    av.rotate()
    keyslots.forget_cached_vmks()
    assert win.resolve("vault://hosts/cybesis-vps/ssh") == SSH_PW


def test_an_unenrolled_client_says_so(tmp_path):
    fed = FederationClient(Vault(tmp_path / "solo"))
    assert not fed.enrolled()
    with pytest.raises(FederationError, match="not enrolled"):
        fed.resolve("vault://hosts/x/ssh")


# ── rate limiting (2026-10-16) ──────────────────────────────────────────────


def test_a_device_over_its_budget_is_told_to_wait_and_keeps_its_cache(world):
    from navig_vault.federation.authority import RATE_PER_DEVICE

    clock, authority, net, device, _ = world
    win = device("neuron-pc")
    authority.grant(_id(win), "host/*", cache_ttl_s=3600)
    for _ in range(RATE_PER_DEVICE):
        win.resolve("vault://hosts/cybesis-vps/ssh")
    with pytest.raises(FederationError, match="rate-limiting") as exc:
        win.resolve("vault://hosts/cybesis-vps/ssh")
    assert not isinstance(exc.value, DeviceRevoked)
    assert win.cache_entries(), "a rate limit is not a revocation"
    clock.t += 61
    assert win.resolve("vault://hosts/cybesis-vps/ssh") == SSH_PW


def test_failed_signatures_are_throttled_per_claimed_device(world):
    from navig_vault.federation.authority import RATE_AUTH_FAILURES

    clock, authority, _, device, _ = world
    win = device("neuron-pc")
    body = b"{}"
    bad = P.sign(_id(win), b"x" * 32, "POST", "/api/vault/v1/status", body, now=clock.t)
    for _ in range(RATE_AUTH_FAILURES):
        bad = P.sign(_id(win), b"x" * 32, "POST", "/api/vault/v1/status", body, now=clock.t)
        assert authority.handle("POST", "/api/vault/v1/status", bad, body, relayed=False)[0] == 401
    bad = P.sign(_id(win), b"x" * 32, "POST", "/api/vault/v1/status", body, now=clock.t)
    status, data = authority.handle("POST", "/api/vault/v1/status", bad, body, relayed=False)
    assert status == 429 and data["retry_after"] >= 1


def test_enrollment_attempts_are_throttled_per_source(world):
    from navig_vault.federation.authority import RATE_ENROLL

    _, authority, _, _, _ = world
    body = json.dumps({"code": "guess"}).encode()
    for _ in range(RATE_ENROLL):
        assert authority.handle("POST", "/api/vault/v1/enroll", {}, body, relayed=False, source="192.0.2.9")[0] == 403
    assert authority.handle("POST", "/api/vault/v1/enroll", {}, body, relayed=False, source="192.0.2.9")[0] == 429
    # another source has its own budget
    assert authority.handle("POST", "/api/vault/v1/enroll", {}, body, relayed=False, source="192.0.2.8")[0] == 403