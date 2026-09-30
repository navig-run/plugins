"""Unit tests for navig-devhost engine — pure logic only (no admin, no binds)."""

from __future__ import annotations

import json

import pytest


# ── net: loopback allocation ──────────────────────────────────────────────────
def test_loopbacks_in_use_parses_hosts_and_reserves_localhost():
    from navig_devhost.engine import net

    content = "127.0.0.2\tapp.test\n127.0.0.5   other.test # navig-devhost\n"
    used = net.loopbacks_in_use(content, "127.0.0.9")
    assert "127.0.0.1" in used  # localhost is always reserved
    assert {"127.0.0.2", "127.0.0.5", "127.0.0.9"} <= used


def test_next_free_loopback_skips_used():
    from navig_devhost.engine import net

    assert net.next_free_loopback("127.0.0.2 a.test\n127.0.0.3 b.test\n") == "127.0.0.4"


def test_next_free_loopback_empty_hosts_starts_at_2():
    from navig_devhost.engine import net

    assert net.next_free_loopback("") == "127.0.0.2"


def test_next_free_loopback_exhausted_raises():
    from navig_devhost.engine import net

    content = "\n".join(f"127.0.0.{i} h{i}.test" for i in range(2, 255))
    with pytest.raises(RuntimeError):
        net.next_free_loopback(content)


# ── registry: url/target + round-trip + resilience ───────────────────────────
def test_devhost_url_and_target_properties():
    from navig_devhost.engine.registry import DevHost

    dh = DevHost(domain="app.test", ip="127.0.0.2", target_port=3000)
    assert dh.url == "https://app.test"  # 443 default omitted
    assert dh.target == "http://127.0.0.1:3000"

    plain = DevHost(domain="x.test", ip="127.0.0.3", target_port=8080, tls=False)
    assert plain.url == "http://x.test:8080"


def test_registry_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr("navig_devhost.engine.registry.registry_path",
                        lambda: tmp_path / "registry.json")
    from navig_devhost.engine.registry import DevHost, Registry

    reg = Registry()
    reg.put(DevHost(domain="app.test", ip="127.0.0.2", target_port=3000))
    reg.save()

    loaded = Registry.load()
    entry = loaded.get("app.test")
    assert entry is not None and entry.ip == "127.0.0.2" and entry.target_port == 3000


def test_registry_load_skips_unknown_keys_and_bad_entries(tmp_path, monkeypatch):
    """A future/foreign field must not crash load (forward-compat), and a
    malformed entry is skipped rather than nuking the whole registry."""
    monkeypatch.setattr("navig_devhost.engine.registry.registry_path",
                        lambda: tmp_path / "registry.json")
    (tmp_path / "registry.json").write_text(json.dumps({
        "version": 99,  # a newer schema this build doesn't know
        "domains": {
            "good.test": {"domain": "good.test", "ip": "127.0.0.2",
                          "target_port": 3000, "future_field": "ignore-me"},
            "bad.test": "not-a-dict",
        },
    }), encoding="utf-8")
    from navig_devhost.engine.registry import Registry

    reg = Registry.load()
    assert reg.get("good.test") is not None  # unknown key ignored, entry kept
    assert reg.get("bad.test") is None       # malformed entry skipped, no crash
