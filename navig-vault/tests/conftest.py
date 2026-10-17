"""Shared fixtures for the navig-vault suite.

Deliberately contains NO ``sys.modules`` eviction. Forcing the standalone branch by
evicting navig is the obvious way to test ``_compat``, and it is banned in this repo for a
real reason: a module imported inside such an eviction outlives teardown and then breaks
dotted ``monkeypatch.setattr`` for every later test in the same xdist worker (#1109). The
guard that enforces that caught the first version of this file.

The fallbacks are exposed as ``_*_fallback`` functions instead, so they can be called
directly with no import trickery — and the one test that has to prove real standalone
behaviour spawns a SUBPROCESS, which cannot disturb this interpreter at all.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every navig path variable so a test starts from the documented default."""
    for var in ("NAVIG_VAULT_DIR", "NAVIG_CONFIG_DIR", "NAVIG_DATA_DIR", "NAVIG_SYSTEM_SERVICE"):
        monkeypatch.delenv(var, raising=False)
