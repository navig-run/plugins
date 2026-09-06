# navig-vault

> A free, standalone, encrypted **developer secrets manager** — Doppler / 1Password-CLI /
> Infisical / Bitwarden class — that installs **without** navig and shares **one vault**.

**Status: complete and standalone.**

`pip install navig-vault` gives you a working `nv` with no navig required:

```sh
pip install navig-vault

export OPENAI_KEY=sk-...
nv add openai --from-env OPENAI_KEY     # never on the command line -- see below
nv get openai                            # the secret, on stdout, and nothing else
nv list                                  # never prints secrets
nv test openai                           # check it against the provider (needs [validators])
nv path                                  # which vault is in use
```

**A secret is never taken as an argument.** There is no `--value` flag and there will not be
one: command-line arguments are visible in shell history and to every other process via `ps`.
Values come from `--from-env`, `--from-file`, `--stdin`, or a non-echoing prompt.

**One vault, shared.** A standalone install resolves the same `~/.navig/vault` navig uses
(honouring `NAVIG_CONFIG_DIR` / `NAVIG_VAULT_DIR`), so adding a credential with `nv` makes it
readable by `navig`, and vice versa. That is the `_compat` seam: it delegates to navig's own
helpers when navig is installed and falls back to behaviour-matched implementations when it
is not.

**As a library:**

```python
from navig_vault import SecretStr, Credential, CredentialType, totp_now, vault_dir
from navig_vault.core import get_vault

secret = get_vault().get_api_key("openai")
```

The package is fully annotated and ships `py.typed`. `navig.vault.<module>` inside navig is a
thin shim that aliases to the module here, so there is exactly one implementation and the two
cannot drift.

## What it will be

- **Install-without-navig:** `pip install navig-vault` → `navig-vault` (alias `nv`) works on its
  own. Add navig later and they share the same encrypted vault automatically.
- **Runtime injection:** `navig-vault run -- <cmd>` injects real secret values from the encrypted
  vault into a process's environment at runtime (no plaintext `.env` on disk).
- **Secret types:** API keys, logins, cards, passport, TOTP.
- **Frictionless unlock:** OS keychain (macOS Keychain, Windows Credential Manager, Linux Secret
  Service) with a passphrase fallback.
- **Apache-2.0**, own subdomain **vault.navig.run**.

## Layout

```
navig-vault/
  pyproject.toml        # name "navig-vault", scripts: navig-vault + nv
  LICENSE               # Apache-2.0
  README.md
  navig_vault/
    __init__.py         # package metadata
    cli.py              # `navig-vault` / `nv` entrypoint  (stub)
    __main__.py         # `python -m navig_vault`
  tests/
    test_smoke.py
```

## Develop

```bash
cd navig-vault
python -m pip install -e ".[dev]"
navig-vault --help      # or:  nv --help   or:  python -m navig_vault
pytest -q
```

## Relationship to navig

Inside navig it remains `navig vault`. `navig-vault` owns the code; `navig-core` re-exports it,
so the two never fork. See the implementation plan (extract-from-navig-core, share-don't-fork)
in the project's Claude plans (`i-ant-to-make-staged-prism`).
