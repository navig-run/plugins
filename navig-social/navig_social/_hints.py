"""Advice that names a command which exists where the user is.

Inside navig the commands are ``navig social …``, ``navig facebook …``, ``navig vault set …`` and
``navig config set …``. On its own (``pip install navig-social``) they are ``navig-social …``,
``navig-social facebook …`` and ``navig-vault add …``, and a non-secret setting is an environment
variable (``NAVIG_<PROVIDER>_<KEY>``, which :func:`navig_social.social.credentials.get_config`
reads first) because there is no ``config`` command without navig.
"""

from __future__ import annotations

from navig_sdk.host import command_name, navig_available

#: `navig social` inside navig, `navig-social` on its own.
CMD = command_name("social")
#: `navig facebook` inside navig; on its own the group lives under `navig-social`.
FB = "navig facebook" if navig_available() else "navig-social facebook"


def vault_set(provider: str, value: str = "<key>") -> str:
    """How to store *provider*'s secret in the shared vault."""
    if navig_available():
        return f"navig vault set {provider} {value}"
    return f"navig-vault add {provider}"


def config_set(dotted: str, value: str) -> str:
    """How to set the non-secret ``adapters.social.<provider>.<key>`` value *dotted*."""
    if navig_available():
        return f"navig config set {dotted} {value}"
    provider, key = dotted.split(".")[-2:]
    return f"set NAVIG_{provider.upper()}_{key.upper()}={value}"
