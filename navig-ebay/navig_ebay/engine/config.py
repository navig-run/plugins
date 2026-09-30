"""Non-secret configuration for navig-ebay.

Secrets (client id/secret, OAuth tokens) live in the navig vault — see
``oauth_ebay``. This module holds only non-secret operational settings
(environment, marketplace, RuName, and default location/policy ids) in an atomic
YAML file so a torn or transient read can never wipe it.

Storage: ``<navig config dir>/navig-ebay/config.yaml``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from navig_sdk.files import atomic_write_yaml, load_yaml_for_update
from navig_sdk.host import config_dir


def _config_path() -> Path:
    """``<navig config dir>/navig-ebay/config.yaml`` — the same file with or without navig.

    The old standalone fallback was ``~/.navig/config``, one directory deeper than navig's
    ``~/.navig``: a user who ran navig-ebay alone and later installed navig got a second,
    empty config. navig-sdk resolves navig's own directory either way.
    """
    return Path(config_dir()) / "navig-ebay" / "config.yaml"


VALID_ENVIRONMENTS = ("sandbox", "production")


@dataclass
class EbayConfig:
    """Non-secret navig-ebay settings."""

    environment: str = "sandbox"  # sandbox | production
    marketplace_id: str = "EBAY_US"
    content_language: str = "en-US"
    ru_name: str | None = None  # the OAuth redirect URL name registered on the keyset
    default_location_key: str | None = None
    default_payment_policy_id: str | None = None
    default_return_policy_id: str | None = None
    default_fulfillment_policy_id: str | None = None
    # informational; the accepted-URL where eBay lands the auth code (for the
    # paste-the-code flow). Purely a hint shown to the user.
    redirect_accepted_url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EbayConfig":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in (data or {}).items() if k in known}
        return cls(**kwargs)


class EbayConfigManager:
    """Load/save :class:`EbayConfig` atomically.

    Reuses navig's ``safe_load_yaml`` / ``atomic_write_yaml`` when available (the
    same atomic temp-file + ``os.replace`` guarantee navig-github relies on), with
    a plain-yaml fallback for standalone tests.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _config_path()

    def load(self) -> EbayConfig:
        data = self._read()
        return EbayConfig.from_dict(data)

    def save(self, config: EbayConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write(config.to_dict())

    def update(self, **changes: Any) -> EbayConfig:
        """Load, apply *changes*, save, return the new config."""
        config = self.load()
        for key, value in changes.items():
            if key == "extra" and isinstance(value, dict):
                config.extra.update(value)
            elif hasattr(config, key):
                setattr(config, key, value)
            else:
                config.extra[key] = value
        self.save(config)
        return config

    # -- IO helpers -------------------------------------------------------
    def _read(self) -> dict[str, Any]:
        # load() feeds save(): reading an unreadable or corrupt file as {} and saving the
        # defaults back would erase the user's settings. load_yaml_for_update returns {}
        # only for a missing or empty file and RAISES otherwise, so the save never happens.
        return load_yaml_for_update(self.path)

    def _write(self, data: dict[str, Any]) -> None:
        # (data, path): the old navig branch called atomic_write_yaml(self.path, data) —
        # arguments reversed — so it always raised into its fallback and never ran.
        atomic_write_yaml(data, self.path)
