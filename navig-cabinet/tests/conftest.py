"""Every test gets its own cabinet dir and cheap scrypt settings.

The real costs (2**17) are what protect a stolen key file; in tests they would only
make the suite slow. The values used are recorded in the key file either way, so
lowering them here changes nothing about the format being tested.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVIG_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("NAVIG_CABINET_DIR", str(tmp_path / "cabinet"))
    # --with-vault reads the vault: pin it to the sandbox and drop the process
    # singleton, which would otherwise keep pointing at whatever vault it first opened.
    monkeypatch.setenv("NAVIG_VAULT_DIR", str(tmp_path / "vault"))
    import navig_vault.core as vault_core

    monkeypatch.setattr(vault_core, "_vault", None)
    for var in ("NAVIG_CABINET_PASSPHRASE", "NAVIG_CABINET_NEW_PASSPHRASE",
                "NAVIG_CABINET_BACKUP_PASSPHRASE"):
        monkeypatch.delenv(var, raising=False)
    from navig_cabinet import bundle, keys

    monkeypatch.setattr(keys, "PASSPHRASE_N", 2**10)
    monkeypatch.setattr(keys, "MACHINE_N", 2**10)
    monkeypatch.setattr(bundle, "BACKUP_N", 2**10)


@pytest.fixture
def root(tmp_path):
    return tmp_path / "cabinet"


@pytest.fixture
def cab(root):
    from navig_cabinet.store import Cabinet

    c = Cabinet.create(root)
    yield c
    c.close()
