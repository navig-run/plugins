"""Security: the command allowlist must actually contain what runs.

`is_safe` only prefix-checks the command, so `run_allowlisted` MUST execute it as
an argv with NO shell — otherwise a command that merely *starts* with a safe
prefix could chain to anything (`ls / ; rm -rf …`, `curl URL | sh`), an RCE for
anyone holding the HMAC secret. Pure-stdlib daemon; import agent.py directly.
"""

from __future__ import annotations

import shlex
import sys

import pytest

import agent


def test_is_safe_prefix():
    assert agent.is_safe("ls /var")
    assert agent.is_safe("df -h")
    assert not agent.is_safe("rm -rf /")
    assert not agent.is_safe("python evil.py")


def test_run_allowlisted_uses_no_shell(monkeypatch):
    calls = {}

    class _R:
        stdout = ""
        stderr = ""
        returncode = 0

    def fake_run(argv, **kw):
        calls["argv"] = argv
        calls["shell"] = kw.get("shell")
        return _R()

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    agent.run_allowlisted("ls / ; rm -rf /tmp/x", 10)
    # No shell, and the chaining operators are inert literal args — never a shell string.
    assert calls["shell"] is False
    assert calls["argv"] == ["ls", "/", ";", "rm", "-rf", "/tmp/x"]


def test_chaining_does_not_execute():
    # A real run: the chained `echo INJECTED` must NOT run — nothing interprets `;`.
    py = shlex.quote(sys.executable)
    r = agent.run_allowlisted(f"{py} -c \"print('a')\" ; echo INJECTED", 10)
    assert r.returncode == 0
    assert "INJECTED" not in r.stdout


def test_malformed_quotes_raise():
    with pytest.raises(ValueError):  # caller maps this to HTTP 400
        agent.run_allowlisted('cat "unbalanced', 10)
