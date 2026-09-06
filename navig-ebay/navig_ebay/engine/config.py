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


def _config_path() -> Path:
    """Resolve the config file path, preferring navig's canonical config dir.

    Falls back to ``~/.navig/config`` if navig core isn't importable (e.g. an
    isolated unit test), so the module never hard-fails on import.
    """
    try:
        from navig.platform.paths import config_dir  # type: ignore

        base = Path(config_dir())
    except Exception:  # pragma: no cover — standalone/test fallback
        base = Path.home() / ".navig" / "config"
    return base / "navig-ebay" / "config.yaml"


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
        if not self.path.exists():
            return {}
        try:
            from navig.core.yaml_io import safe_load_yaml  # type: ignore

            return safe_load_yaml(self.path) or {}
        except Exception:  # pragma: no cover — fallback
            import yaml

            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            except Exception:
                return {}

    def _write(self, data: dict[str, Any]) -> None:
        try:
            from navig.core.yaml_io import atomic_write_yaml  # type: ignore

            atomic_write_yaml(self.path, data)
            return
        except Exception:  # pragma: no cover — fallback
            import os

            import yaml

            tmp = self.path.with_name(self.path.name + ".navig-tmp")
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    yaml.dump(data, f, default_flow_style=False, sort_keys=False)
                os.replace(tmp, self.path)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
