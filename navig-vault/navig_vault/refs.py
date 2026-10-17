"""``vault://`` references — one grammar for config files, the CLI and remote clients.

    vault://<path>[#<field>][?v=<version>]

``<path>`` is a vault label, with friendly aliases for host credentials:

    vault://hosts/<host>/ssh      → host/<host>/ssh_password
    vault://hosts/<host>/db       → host/<host>/database.password
    vault://hosts/<host>/db-root  → host/<host>/database.root_password
    vault://hosts/<host>/<key>    → host/<host>/<key>

``#<field>`` picks one key of a JSON payload (default: the item's primary
secret, the way ``get_secret`` chooses it). ``?v=<N>`` reads a replaced version
from the item history.

The older ``vault:<label>`` form (what host YAML ``*_ref`` fields hold) parses
too, so there is exactly one place a reference becomes a label.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote

__all__ = ["VaultRef", "is_ref", "parse_ref", "resolve_ref"]

SCHEME = "vault://"
LEGACY_PREFIX = "vault:"

_HOST_ALIASES = {
    "ssh": "ssh_password",
    "db": "database.password",
    "db-root": "database.root_password",
}
_LABEL_OK = re.compile(r"^[A-Za-z0-9_.@:/+-]+$")


@dataclass(frozen=True)
class VaultRef:
    label: str
    field: str | None = None
    version: int | None = None

    def uri(self) -> str:
        out = SCHEME + self.label
        if self.field:
            out += f"#{self.field}"
        if self.version is not None:
            out += f"?v={self.version}"
        return out


def is_ref(text: object) -> bool:
    return isinstance(text, str) and (text.startswith(SCHEME) or text.startswith(LEGACY_PREFIX))


def _label_for(path: str) -> str:
    parts = path.split("/")
    if len(parts) == 3 and parts[0] == "hosts" and parts[1]:
        return f"host/{parts[1]}/{_HOST_ALIASES.get(parts[2], parts[2])}"
    return path


def parse_ref(text: str) -> VaultRef:
    """Parse ``vault://…`` or ``vault:<label>``. Raises ``ValueError`` on anything else."""
    if not isinstance(text, str):
        raise ValueError("a vault reference must be a string")
    if text.startswith(SCHEME):
        body = text[len(SCHEME) :]
        version = None
        if "?" in body:
            body, _, query = body.partition("?")
            values = parse_qs(query).get("v")
            if values:
                try:
                    version = int(values[0])
                except ValueError as exc:
                    raise ValueError(f"bad version in vault reference: {values[0]!r}") from exc
                if version < 1:
                    raise ValueError("vault reference versions start at 1")
        field = None
        if "#" in body:
            body, _, field = body.partition("#")
            field = unquote(field) or None
        path = unquote(body).strip("/")
        label = _label_for(path)
    elif text.startswith(LEGACY_PREFIX):
        label, field, version = text[len(LEGACY_PREFIX) :], None, None
    else:
        raise ValueError("not a vault reference (expected vault://… or vault:…)")
    if not label or not _LABEL_OK.match(label) or ".." in label.split("/"):
        raise ValueError("vault reference has an empty or invalid path")
    return VaultRef(label=label, field=field, version=version)


def resolve_ref(vault, ref: VaultRef | str) -> str:
    """The plaintext *ref* points at. Raises ``KeyError`` (absent) / ``ValueError`` (no such field)."""
    if isinstance(ref, str):
        ref = parse_ref(ref)
    if ref.field is None and ref.version is None:
        return vault.get_secret(ref.label).reveal()
    payload = (
        vault.get_bytes(ref.label)
        if ref.version is None
        else vault.get_bytes(ref.label, version=ref.version)
    )
    if ref.field is None:
        from .core import _decode_payload  # noqa: PLC0415

        data = _decode_payload(payload)
        for key in ("value", "api_key", "token", "password", "secret"):
            if key in data:
                return str(data[key])
        first = next(iter(data.values()), "")
        return str(first)
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"{ref.label!r} is not a JSON item; it has no field {ref.field!r}") from exc
    if not isinstance(data, dict) or ref.field not in data:
        raise ValueError(f"{ref.label!r} has no field {ref.field!r}")
    return str(data[ref.field])
