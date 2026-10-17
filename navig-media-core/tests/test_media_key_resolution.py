"""One media key resolver; each caller keeps the precedence it always had."""

from __future__ import annotations

import sys
import types

import pytest

from navig_media_core.tools import media_providers as MP


class _FakeVault:
    def __init__(self, api: dict[str, str] | None = None, secrets: dict[str, str] | None = None,
                 broken: bool = False) -> None:
        self.api, self.secrets, self.broken = api or {}, secrets or {}, broken
        self.callers: list[str] = []

    def get_api_key(self, provider: str, profile_id=None, caller: str = "unknown"):
        self.callers.append(caller)
        if self.broken:
            raise RuntimeError("vault locked")
        return self.api.get(provider)

    def get_secret(self, label: str):
        return self.secrets.get(label)


@pytest.fixture
def vault(monkeypatch):
    holder = {"v": _FakeVault()}
    mod = types.ModuleType("navig_vault.core")
    mod.get_vault = lambda: holder["v"]
    pkg = types.ModuleType("navig_vault")
    pkg.core = mod
    monkeypatch.setitem(sys.modules, "navig_vault", pkg)
    monkeypatch.setitem(sys.modules, "navig_vault.core", mod)
    monkeypatch.delenv("X_KEY", raising=False)
    monkeypatch.delenv("Y_KEY", raising=False)
    return holder


def test_env_first_by_default(vault, monkeypatch) -> None:
    vault["v"] = _FakeVault(api={"p": "from-vault"})
    monkeypatch.setenv("X_KEY", "from-env")
    assert MP.resolve_media_key("p", "X_KEY") == "from-env"


def test_vault_first_when_asked(vault, monkeypatch) -> None:
    vault["v"] = _FakeVault(api={"p": "from-vault"})
    monkeypatch.setenv("X_KEY", "from-env")
    assert MP.resolve_media_key("p", "X_KEY", prefer="vault") == "from-vault"


def test_falls_through_to_the_other_source(vault, monkeypatch) -> None:
    monkeypatch.setenv("Y_KEY", " from-env \n")
    assert MP.resolve_media_key("p", "X_KEY", "Y_KEY", prefer="vault") == "from-env"
    vault["v"] = _FakeVault(api={"p": "from-vault"})
    monkeypatch.delenv("Y_KEY")
    assert MP.resolve_media_key("p", "X_KEY") == "from-vault"


def test_legacy_labels_only_when_passed(vault) -> None:
    vault["v"] = _FakeVault(secrets={"p/api_key": "legacy"})
    assert MP.resolve_media_key("p") is None  # generation never read labels
    assert MP.resolve_media_key("p", labels=("p", "p/api_key")) == "legacy"


def test_a_broken_vault_is_not_configured_not_a_crash(vault, monkeypatch) -> None:
    vault["v"] = _FakeVault(broken=True, secrets={"p": "legacy"})
    assert MP.resolve_media_key("p", labels=("p",), prefer="vault") == "legacy"
    monkeypatch.setenv("X_KEY", "from-env")
    assert MP.resolve_media_key("p", "X_KEY", prefer="vault") == "from-env"


def test_blank_values_never_count(vault, monkeypatch) -> None:
    vault["v"] = _FakeVault(api={"p": "   "})
    monkeypatch.setenv("X_KEY", "  ")
    assert MP.resolve_media_key("p", "X_KEY") is None


def test_caller_reaches_the_vault_access_log(vault) -> None:
    vault["v"] = _FakeVault(api={"p": "k"})
    MP.resolve_media_key("p", caller="voice_tts")
    assert vault["v"].callers == ["voice_tts"]


def test_unknown_preference_is_an_error(vault) -> None:
    with pytest.raises(ValueError):
        MP.resolve_media_key("p", prefer="both")
