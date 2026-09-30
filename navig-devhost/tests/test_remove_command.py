"""`navig devhost remove` must stay consistent on a partial failure.

If `hosts.remove()` can't remove the entry (not elevated / a write error), the cert files
and the registry record must be PRESERVED — deleting them while the live hosts entry
survives orphans the domain (it still resolves to a loopback, nothing serving it) and
throws away the state needed to retry. `add` aborts on a hosts failure; remove must mirror it.
"""
from __future__ import annotations

import pytest
from typer.testing import CliRunner


@pytest.fixture
def registry_at(tmp_path, monkeypatch):
    """Point the registry at an isolated temp file."""
    rp = tmp_path / "registry.json"
    monkeypatch.setattr("navig_devhost.engine.registry.registry_path", lambda: rp)
    return rp


def _seed(cert, key):
    from navig_devhost.engine.registry import DevHost, Registry

    reg = Registry()
    reg.put(DevHost(domain="app.test", ip="127.0.0.2", target_port=3000,
                    cert=str(cert), key=str(key)))
    reg.save()


def _cert_key(tmp_path):
    cert = tmp_path / "app.test.pem"
    cert.write_text("CERT", encoding="utf-8")
    key = tmp_path / "app.test-key.pem"
    key.write_text("KEY", encoding="utf-8")
    return cert, key


def test_remove_preserves_state_when_hosts_removal_fails(registry_at, tmp_path, monkeypatch):
    from navig_devhost.commands.devhost import devhost_app
    from navig_devhost.engine import hosts
    from navig_devhost.engine.registry import Registry

    cert, key = _cert_key(tmp_path)
    _seed(cert, key)

    # hosts removal fails (e.g. not elevated) — the entry stays in the hosts file.
    monkeypatch.setattr(
        hosts, "remove",
        lambda d: hosts.HostsResult(False, "admin privileges required to edit the hosts file"),
    )

    res = CliRunner().invoke(devhost_app, ["remove", "app.test"])

    assert res.exit_code == 2, res.output          # aborted, not a silent teardown
    assert cert.exists() and key.exists()          # cert files preserved for retry
    assert Registry.load().get("app.test") is not None  # registry record preserved


def test_remove_cleans_up_on_success(registry_at, tmp_path, monkeypatch):
    from navig_devhost.commands.devhost import devhost_app
    from navig_devhost.engine import hosts
    from navig_devhost.engine.registry import Registry

    cert, key = _cert_key(tmp_path)
    _seed(cert, key)
    monkeypatch.setattr(hosts, "remove", lambda d: hosts.HostsResult(True, "removed app.test"))

    res = CliRunner().invoke(devhost_app, ["remove", "app.test"])

    assert res.exit_code == 0, res.output
    assert not cert.exists() and not key.exists()  # certs cleaned up
    assert Registry.load().get("app.test") is None  # registry record removed


def test_remove_keep_cert_leaves_files_on_success(registry_at, tmp_path, monkeypatch):
    from navig_devhost.commands.devhost import devhost_app
    from navig_devhost.engine import hosts
    from navig_devhost.engine.registry import Registry

    cert, key = _cert_key(tmp_path)
    _seed(cert, key)
    monkeypatch.setattr(hosts, "remove", lambda d: hosts.HostsResult(True, "removed"))

    res = CliRunner().invoke(devhost_app, ["remove", "app.test", "--keep-cert"])

    assert res.exit_code == 0, res.output
    assert cert.exists() and key.exists()          # --keep-cert leaves them
    assert Registry.load().get("app.test") is None
