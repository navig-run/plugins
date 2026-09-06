"""navig-vault -- a free, standalone, encrypted developer secrets manager.

The vault is no longer *being* extracted from navig: it lives here. All 17 modules -- the
types, SecretStr, TOTP, the crypto and storage engine, the provider registry, the credential
validators, and the orchestration (``core``, ``resolver``, ``sessions``, ``logins``,
``migrate``, ``manager``) -- are this package's code. What navig keeps at
``navig.vault.<module>`` are thin shims that ALIAS themselves to the modules here
(``sys.modules[__name__] = _impl``), so there is exactly one implementation and the two
cannot drift. A guard in navig's suite asserts the module objects are identical.

Everything imports with navig absent; the seam that makes that true is ``_compat``, which
delegates to navig's own helpers when navig is installed and falls back to behaviour-matched
implementations when it is not -- so a standalone install reads the SAME ``~/.navig/vault``
rather than a second one.

    from navig_vault import SecretStr, Credential, CredentialType, totp_now, vault_dir
    from navig_vault.core import get_vault

Optional extras: ``argon2`` (Argon2id KDF; PBKDF2 at 600k is the fallback), ``keyring``
(OS keyring for the master key), ``validators`` (httpx, for provider credential health
checks), ``web`` (tldextract, for registrable-domain login binding).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

__all__ = [
    "__version__",
    "Credential",
    "CredentialType",
    "SecretStr",
    "totp_now",
    "is_valid_secret",
    "mask_secret",
    "vault_dir",
]

__version__ = "0.7.0"

if TYPE_CHECKING:  # pragma: no cover - import-time cost is the whole point of the lazy path
    from ._compat import vault_dir
    from .secret_str import SecretStr, mask_secret
    from .totp import is_valid_secret, totp_now
    from .types import Credential, CredentialType


def __getattr__(name: str):
    """Resolve the public surface on first use.

    Deliberately lazy: ``types`` pulls in dataclasses/enum and ``totp`` pulls in hmac and
    struct, and a caller who only wants ``SecretStr`` should not pay for either. The eager
    alternative also makes ``import navig_vault`` fail on a broken optional dependency,
    which is exactly the failure mode the vault must not have -- it is what other packages
    reach for to READ a secret.
    """
    if name in ("Credential", "CredentialType"):
        from . import types

        return getattr(types, name)
    if name in ("SecretStr", "mask_secret"):
        from . import secret_str

        return getattr(secret_str, name)
    if name in ("totp_now", "is_valid_secret"):
        from . import totp

        return getattr(totp, name)
    if name == "vault_dir":
        from ._compat import vault_dir

        return vault_dir
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
