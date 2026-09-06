"""Unit tests for navig-devhost's hosts-file editor (safety-sensitive: it edits
the shared system hosts file)."""

from __future__ import annotations

from pathlib import Path

import pytest

from navig_devhost.engine import hosts


class _FakeOps:
    """A hosts backend over a temp file (admin allowed, real read/write)."""

    def __init__(self, path: Path):
        self._path = path

    def get_hosts_file_path(self):
        return self._path

    def can_edit_hosts_file(self):
        return True

    def read_hosts_file(self):
        try:
            return self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return f"Hosts file not found at {self._path}"


@pytest.fixture
def hostsfile(tmp_path, monkeypatch):
    hp = tmp_path / "hosts"
    monkeypatch.setattr(hosts, "_ops", lambda: _FakeOps(hp))
    return hp


# ── read(): error sentinels are never treated as content ─────────────────────
def test_read_guards_not_found_sentinel(monkeypatch, tmp_path):
    monkeypatch.setattr(hosts, "_ops", lambda: _FakeOps(tmp_path / "nope"))
    assert hosts.read() == ""  # a missing hosts file must not read as content


def test_read_guards_permission_denied(monkeypatch):
    class _Denied:
        def read_hosts_file(self):
            return "Permission denied reading /etc/hosts"

    monkeypatch.setattr(hosts, "_ops", lambda: _Denied())
    assert hosts.read() == ""


# ── add() ────────────────────────────────────────────────────────────────────
def test_add_then_idempotent(hostsfile):
    hostsfile.write_text("127.0.0.1\tlocalhost\n", encoding="utf-8")
    assert hosts.add("127.0.0.2", "foo.test").ok
    txt = hostsfile.read_text()
    assert "127.0.0.2\tfoo.test\t# navig-devhost" in txt
    assert "localhost" in txt  # existing line untouched
    assert hosts.add("127.0.0.2", "foo.test").ok  # idempotent
    assert hostsfile.read_text().count("foo.test") == 1


def test_add_rejects_conflicting_ip(hostsfile):
    hostsfile.write_text("127.0.0.2\tfoo.test\t# navig-devhost\n", encoding="utf-8")
    res = hosts.add("127.0.0.9", "foo.test")
    assert not res.ok and "already maps" in res.message


# ── remove() ─────────────────────────────────────────────────────────────────
def test_remove_only_the_target_domain(hostsfile):
    hostsfile.write_text(
        "127.0.0.1\tlocalhost\n"
        "127.0.0.2\ta.test\t# navig-devhost\n"
        "127.0.0.3\tb.test\t# navig-devhost\n",
        encoding="utf-8",
    )
    assert hosts.remove("a.test").ok
    txt = hostsfile.read_text()
    assert "a.test" not in txt
    assert "b.test" in txt and "localhost" in txt  # everything else preserved


def test_remove_absent_is_noop(hostsfile):
    hostsfile.write_text("127.0.0.1\tlocalhost\n", encoding="utf-8")
    res = hosts.remove("ghost.test")
    assert res.ok and "not in hosts file" in res.message
    assert hostsfile.read_text() == "127.0.0.1\tlocalhost\n"  # untouched


def test_remove_reads_once_never_truncates(hostsfile, monkeypatch):
    """remove() rewrites from the SAME snapshot it checked. If a later read went
    empty (a TOCTOU window in the old multi-read code), the file must NOT be
    truncated — localhost and other entries survive."""
    hostsfile.write_text(
        "127.0.0.1\tlocalhost\n127.0.0.2\tfoo.test\t# navig-devhost\n", encoding="utf-8"
    )
    real = hostsfile.read_text(encoding="utf-8")
    calls = {"n": 0}

    def flaky_read():
        calls["n"] += 1
        return real if calls["n"] == 1 else ""  # any 2nd read would be empty

    monkeypatch.setattr(hosts, "read", flaky_read)
    assert hosts.remove("foo.test").ok
    out = hostsfile.read_text()
    assert "localhost" in out  # not truncated
    assert "foo.test" not in out
    assert calls["n"] == 1  # read exactly once
