"""Regression tests for the social credential lookup.

The vault's SecretStr self-redacts in ``str()`` (returns ``***``) and exposes
the real value only via ``.reveal()``. ``get_token`` once fell back to
``str(secret)`` and handed the literal mask to every publisher — these tests
pin the unwrap order: env var → reveal() → get_secret_value().
"""
from __future__ import annotations

import sys
import types

from navig_social.social import credentials


class _RevealSecret:
    """Mimics navig.vault.secret_str.SecretStr: str() redacts, reveal() unmasks."""

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __str__(self) -> str:  # pragma: no cover — the trap the bug fell into
        return "***"


class _PydanticSecret:
    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value


def _stub_vault(monkeypatch, secret_obj) -> None:
    fake_core = types.ModuleType("navig.vault.core")

    class _Vault:
        def get_secret(self, provider):
            return secret_obj

    fake_core.get_vault = lambda: _Vault()
    monkeypatch.setitem(sys.modules, "navig.vault.core", fake_core)


def test_get_token_reveals_vault_secret_not_the_mask(monkeypatch):
    monkeypatch.delenv("NAVIG_DEVTO_TOKEN", raising=False)
    monkeypatch.delenv("DEVTO_TOKEN", raising=False)
    monkeypatch.delenv("DEVTO_ACCESS_TOKEN", raising=False)
    _stub_vault(monkeypatch, _RevealSecret("real-token"))
    assert credentials.get_token("devto") == "real-token"


def test_get_token_supports_pydantic_style_secrets(monkeypatch):
    monkeypatch.delenv("NAVIG_DEVTO_TOKEN", raising=False)
    monkeypatch.delenv("DEVTO_TOKEN", raising=False)
    monkeypatch.delenv("DEVTO_ACCESS_TOKEN", raising=False)
    _stub_vault(monkeypatch, _PydanticSecret("pyd-token"))
    assert credentials.get_token("devto") == "pyd-token"


def test_env_var_wins_over_vault(monkeypatch):
    _stub_vault(monkeypatch, _RevealSecret("vault-token"))
    monkeypatch.setenv("NAVIG_DEVTO_TOKEN", "env-token")
    assert credentials.get_token("devto") == "env-token"
