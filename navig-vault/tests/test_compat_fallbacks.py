"""The ``_compat`` seam must work with navig absent, and agree with navig when present.

The fallbacks are called DIRECTLY (``_vault_dir_fallback`` and friends) rather than forced
by evicting navig from ``sys.modules`` — that eviction is banned here because a module
imported inside one outlives teardown and breaks dotted ``monkeypatch.setattr`` for every
later test in the same xdist worker. Calling the function needs no such trick.

One test still has to prove the real thing — that the package imports and resolves with
navig genuinely unimportable — and it does that in a SUBPROCESS, which cannot disturb this
interpreter.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from navig_vault import _compat


# ── the real standalone proof, out of process ────────────────────────────────


def test_imports_and_resolves_with_navig_unimportable(tmp_path: Path) -> None:
    """A subprocess where `import navig` raises — the bare `pip install navig-vault` case."""
    pkg_root = Path(_compat.__file__).resolve().parents[1].parent
    script = textwrap.dedent(
        f"""
        import sys, importlib.abc
        sys.path.insert(0, {str(pkg_root)!r})

        class Block(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name == "navig" or name.startswith("navig."):
                    raise ModuleNotFoundError(name)
                return None

        sys.meta_path.insert(0, Block())

        # Prove the block is real — otherwise everything below passes vacuously.
        try:
            import navig
            raise SystemExit("BLOCK FAILED: navig was importable")
        except ModuleNotFoundError:
            pass

        from navig_vault import _compat
        assert _compat.navig_available() is False
        import os
        os.environ["NAVIG_VAULT_DIR"] = {str(tmp_path / "v")!r}
        assert _compat.vault_dir() == __import__("pathlib").Path({str(tmp_path / "v")!r})
        _compat.set_owner_only_file_permissions({str(tmp_path / "nope")!r})   # must not raise
        assert _compat.safe_json_loads('{{"a": 1}}', {{}}) == {{"a": 1}}
        p = __import__("pathlib").Path({str(tmp_path / "w.txt")!r})
        _compat.atomic_write_text(p, "ok")
        assert p.read_text(encoding="utf-8") == "ok"
        print("STANDALONE OK")
        """
    )
    r = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )
    assert "STANDALONE OK" in r.stdout, f"stdout={r.stdout!r} stderr={r.stderr!r}"


# ── paths: the fallback branch, called directly ──────────────────────────────


def test_vault_dir_honours_the_explicit_override(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("NAVIG_VAULT_DIR", str(tmp_path / "v"))
    assert _compat._vault_dir_fallback() == tmp_path / "v"


def test_vault_dir_falls_back_to_config_dir(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("NAVIG_CONFIG_DIR", str(tmp_path / "cfg"))
    assert _compat._vault_dir_fallback() == tmp_path / "cfg" / "vault"


def test_vault_override_beats_config_dir(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("NAVIG_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("NAVIG_VAULT_DIR", str(tmp_path / "v"))
    assert _compat._vault_dir_fallback() == tmp_path / "v"


def test_default_is_the_user_home_vault(clean_env):
    assert _compat._vault_dir_fallback() == Path.home() / ".navig" / "vault"


def test_system_service_branch_is_not_forgotten(clean_env, monkeypatch):
    """The leg navig_blackbox's _compat drops.

    A root-run service must resolve /etc/navig/vault, not ~/.navig/vault — otherwise it
    silently opens a DIFFERENT, empty vault while navig uses the real one.
    """
    monkeypatch.setenv("NAVIG_SYSTEM_SERVICE", "1")
    assert _compat._config_dir_fallback() == Path("/etc/navig")
    assert _compat._vault_dir_fallback() == Path("/etc/navig") / "vault"


def test_the_fallback_agrees_with_navig(clean_env, monkeypatch, tmp_path):
    """Same inputs, same answer, fallback vs navig — the shared-vault promise.

    Skipped in the standalone mirror, where navig genuinely is not installed; there the
    subprocess test above is what covers this side.
    """
    paths = pytest.importorskip("navig.platform.paths", reason="navig not installed (mirror)")

    for env in (
        {},
        {"NAVIG_VAULT_DIR": str(tmp_path / "a")},
        {"NAVIG_CONFIG_DIR": str(tmp_path / "b")},
        {"NAVIG_CONFIG_DIR": str(tmp_path / "b"), "NAVIG_VAULT_DIR": str(tmp_path / "c")},
    ):
        for var in ("NAVIG_VAULT_DIR", "NAVIG_CONFIG_DIR"):
            monkeypatch.delenv(var, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        assert _compat._vault_dir_fallback() == paths.vault_dir(), f"divergence for {env}"


# ── file permissions ─────────────────────────────────────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_owner_only_permissions_posix(tmp_path):
    f = tmp_path / "vault.salt"
    f.write_bytes(b"salt")
    f.chmod(0o644)
    _compat._set_owner_only_fallback(f)
    assert stat.S_IMODE(f.stat().st_mode) & 0o077 == 0, "group/other must have no access"


def test_owner_only_permissions_never_raises(tmp_path):
    """Best-effort by contract — a permission failure must not take the vault down."""
    _compat._set_owner_only_fallback(tmp_path / "does-not-exist")


# ── atomic write ─────────────────────────────────────────────────────────────


def test_atomic_write_roundtrip(tmp_path):
    p = tmp_path / "nested" / "f.txt"
    _compat._atomic_write_fallback(p, "héllo\n")
    assert p.read_text(encoding="utf-8") == "héllo\n"


def test_atomic_write_leaves_no_temp_file(tmp_path):
    p = tmp_path / "f.txt"
    _compat._atomic_write_fallback(p, "x")
    assert [q.name for q in tmp_path.iterdir()] == ["f.txt"]


def test_atomic_write_replaces_existing(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("old", encoding="utf-8")
    _compat._atomic_write_fallback(p, "new")
    assert p.read_text(encoding="utf-8") == "new"


# ── json ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected", "default"),
    [
        ('{"a": 1}', {"a": 1}, {}),
        ("", {}, {}),
        (None, {}, {}),
        ("not json at all", {}, {}),      # malformed must degrade, not raise
        ("[1, 2]", [1, 2], []),
    ],
)
def test_safe_json_loads(raw, expected, default):
    assert _compat._safe_json_loads_fallback(raw, default) == expected


def test_safe_json_loads_matches_navig():
    """With navig present, the fallback must return exactly what core returns."""
    core_impl = pytest.importorskip("navig.core.json_io", reason="navig not installed").safe_json_loads
    for raw in ('{"a": 1}', "", None, "garbage"):
        assert _compat._safe_json_loads_fallback(raw, {}) == core_impl(raw, {})


def test_valid_input_is_actually_parsed():
    """Guard against the fallback quietly returning the default for VALID input."""
    assert _compat._safe_json_loads_fallback(json.dumps({"k": "v"}), {}) == {"k": "v"}
