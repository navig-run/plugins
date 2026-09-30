"""A cabinet backup can carry the navig vault, and restore it on another machine.

The vault seals every secret under a machine-bound key and has no export of its own,
so without this a dead disk loses every API key. "Another machine" is modelled as a
different vault directory: a different salt derives a different master key, which is
exactly what a different fingerprint does.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile

import pytest
from navig_cabinet import bundle
from navig_cabinet.store import Cabinet
from navig_cabinet.vault_bridge import export_vault, import_vault
from navig_vault.core import Vault
from navig_vault.types import VaultItemKind

SECRET = b"sk-live-UNIQUE-7f3a9c-secret-value"


@pytest.fixture
def home_vault(tmp_path):
    v = Vault(tmp_path / "vault-home")
    v.put("openai/default", SECRET, kind=VaultItemKind.PROVIDER, provider="openai",
          metadata={"profile_id": "default", "enabled": True})
    v.put("github-token", b"ghp_other", kind=VaultItemKind.TOKEN)
    v.put("svc.json", json.dumps({"k": "v"}).encode(), kind=VaultItemKind.JSON)
    return v


def _backup(cab, home_vault, passphrase="pw"):
    exp = export_vault(home_vault.vault_dir)
    assert exp.unreadable == []
    buf = io.BytesIO()
    bundle.write_backup(cab, cab.items(), buf, passphrase, vault_entries=exp.entries)
    return buf.getvalue()


def test_vault_round_trips_to_another_machine(cab, home_vault, tmp_path):
    raw = _backup(cab, home_vault)
    st = bundle.restore_backup(Cabinet.create(tmp_path / "cab2"), io.BytesIO(raw), "pw")
    assert st.vault_entries is not None and len(st.vault_entries) == 3

    other = tmp_path / "vault-other"
    vst = import_vault(st.vault_entries, other)
    assert (vst.restored, vst.skipped, vst.errors) == (3, 0, [])
    v2 = Vault(other)
    assert v2.engine().derive_key(None) != home_vault.engine().derive_key(None)  # really re-sealed
    assert v2.get_bytes("openai/default") == SECRET
    for item in home_vault.list():
        r = v2.store().get(item.label)
        assert (r.id, r.kind, r.provider, r.metadata) == (item.id, item.kind, item.provider, item.metadata)


def test_restore_never_overwrites_a_secret_already_here(cab, home_vault, tmp_path):
    raw = _backup(cab, home_vault)
    st = bundle.restore_backup(Cabinet.create(tmp_path / "cab2"), io.BytesIO(raw), "pw")
    other = tmp_path / "vault-other"
    Vault(other).put("openai/default", b"newer-local-value")
    vst = import_vault(st.vault_entries, other)
    assert (vst.restored, vst.skipped) == (2, 1)
    assert Vault(other).get_bytes("openai/default") == b"newer-local-value"
    again = import_vault(st.vault_entries, other)
    assert (again.restored, again.skipped) == (0, 3)


def test_secrets_are_not_in_the_backup_file_in_plaintext(cab, home_vault):
    raw = _backup(cab, home_vault)
    for needle in (SECRET, b"ghp_other", b"openai/default"):
        assert needle not in raw


def test_wrong_passphrase_yields_no_secrets(cab, home_vault, tmp_path):
    raw = _backup(cab, home_vault, "right")
    with pytest.raises(bundle.BackupError, match="wrong passphrase"):
        bundle.restore_backup(Cabinet.create(tmp_path / "cab2"), io.BytesIO(raw), "wrong")


def test_a_backup_without_the_vault_carries_none(cab, tmp_path):
    buf = io.BytesIO()
    bundle.write_backup(cab, cab.items(), buf, "pw")
    st = bundle.restore_backup(Cabinet.create(tmp_path / "cab2"), io.BytesIO(buf.getvalue()), "pw")
    assert st.vault_entries is None


def test_recovery_script_exposes_the_vault_without_navig(cab, home_vault, tmp_path):
    bak = tmp_path / "b.ncab"
    bak.write_bytes(_backup(cab, home_vault))
    out = tmp_path / "rec.tar"
    subprocess.run([sys.executable, "-I", str(bundle.recovery_script()), str(bak), str(out)],
                   check=True, timeout=120, env=dict(os.environ, NCAB_PASSPHRASE="pw"),
                   stdin=subprocess.DEVNULL)
    with tarfile.open(out) as tar:
        data = json.loads(tar.extractfile(bundle.VAULT_PATH).read())
    import base64

    by_label = {e["label"]: base64.b64decode(e["payload"]) for e in data["items"]}
    assert by_label["openai/default"] == SECRET


def test_cli_with_vault_backs_up_and_restores_the_vault(tmp_path, monkeypatch):
    from navig_cabinet.commands.cabinet import cabinet_app
    from typer.testing import CliRunner

    import navig_vault.core as vault_core

    Vault(tmp_path / "vault").put("anthropic/default", SECRET)  # NAVIG_VAULT_DIR (conftest)
    env = {"NAVIG_CABINET_BACKUP_PASSPHRASE": "bpw"}
    runner = CliRunner()
    bak = tmp_path / "all.ncab"

    res = runner.invoke(cabinet_app, ["backup", "-o", str(bak)], env=env)
    assert res.exit_code == 1 and "no cabinet yet" in res.output  # nothing asked for, nothing to open
    res = runner.invoke(cabinet_app, ["backup", "-o", str(bak), "--with-vault"], env=env)
    assert res.exit_code == 0, res.output
    assert "1 vault secret" in res.output

    # a new machine: fresh cabinet + fresh vault
    monkeypatch.setenv("NAVIG_CABINET_DIR", str(tmp_path / "cab-new"))
    monkeypatch.setenv("NAVIG_VAULT_DIR", str(tmp_path / "vault-new"))
    monkeypatch.setattr(vault_core, "_vault", None)
    res = runner.invoke(cabinet_app, ["restore", str(bak), "--skip-vault"], env=env)
    assert res.exit_code == 0 and "skipped" in res.output
    assert Vault(tmp_path / "vault-new").store().get("anthropic/default") is None
    monkeypatch.setattr(vault_core, "_vault", None)
    res = runner.invoke(cabinet_app, ["restore", str(bak)], env=env)
    assert res.exit_code == 0, res.output
    assert "restored 1 secret" in res.output
    assert Vault(tmp_path / "vault-new").get_bytes("anthropic/default") == SECRET
