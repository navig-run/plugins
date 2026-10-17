"""Every test runs against throwaway locations — never the operator's live monitor state.

Measured 2026-10-16: the route tests isolated the run STORE but not the offline SPOOL.
Starting a test app starts the reaper, which drained the operator's real
``~/.navig/agent-monitor/spool.jsonl`` (live Claude Code hook events) into a temp DB that
was then thrown away. Config (spool, gateway.json), store and logs are all redirected here.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_navig_locations(monkeypatch, tmp_path_factory):
    root = tmp_path_factory.mktemp("navig-isolated")
    monkeypatch.setenv("NAVIG_CONFIG_DIR", str(root / "config"))
    monkeypatch.setenv("NAVIG_STORE_DIR", str(root / "store"))
    monkeypatch.setenv("NAVIG_LOG_DIR", str(root / "logs"))
    monkeypatch.delenv("NAVIG_AGENT_MONITOR_URL", raising=False)
    yield root
