"""The devhost registry — the set of local dev domains devhost manages.

A small JSON file (`<navig config>/devhost/registry.json`) mapping each domain to
its loopback IP, proxy target, and TLS material. This is the source of truth that
`list`, `up`, `status`, and `remove` read.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Optional

from navig.core.json_io import JsonReadError, atomic_write_json, load_json_for_update, load_json_safe
from navig_devhost.engine.paths import registry_path

__all__ = ["DevHost", "Registry", "JsonReadError"]

REGISTRY_VERSION = 1


@dataclass
class DevHost:
    domain: str
    ip: str
    target_host: str = "127.0.0.1"
    target_port: int = 3000
    https_port: int = 443
    tls: bool = True
    cert: Optional[str] = None
    key: Optional[str] = None
    created: Optional[str] = None
    note: str = ""

    @property
    def url(self) -> str:
        scheme = "https" if self.tls else "http"
        port = self.https_port if self.tls else self.target_port
        default = 443 if self.tls else 80
        suffix = "" if port == default else f":{port}"
        return f"{scheme}://{self.domain}{suffix}"

    @property
    def target(self) -> str:
        return f"http://{self.target_host}:{self.target_port}"


@dataclass
class Registry:
    version: int = REGISTRY_VERSION
    domains: dict[str, DevHost] = field(default_factory=dict)

    # ── io ──────────────────────────────────────────────────────────────────
    @classmethod
    def _from_raw(cls, raw: dict) -> "Registry":
        """Build a Registry from a parsed registry.json mapping.

        Filters each record to known fields (forward-compat: a newer devhost may
        add a field this version doesn't know) and skips a malformed entry instead
        of letting one bad record crash the whole registry for list/up/remove.
        """
        known = {f.name for f in fields(DevHost)}
        domains: dict[str, DevHost] = {}
        for name, data in (raw.get("domains") or {}).items():
            if not isinstance(data, dict):
                continue
            filtered = {k: v for k, v in data.items() if k in known}
            filtered["domain"] = data.get("domain", name)
            try:
                domains[name] = DevHost(**filtered)
            except (TypeError, ValueError):
                continue
        return cls(version=raw.get("version", REGISTRY_VERSION), domains=domains)

    @classmethod
    def load(cls) -> "Registry":
        """Read-only / degrading load for list / up / status views.

        An unreadable (transient lock) or corrupt file yields an EMPTY registry,
        never a crash — a view must not blow up. A read-MODIFY-WRITE caller must
        use :meth:`load_for_update` instead: degrading to empty here and then
        :meth:`save`-ing would WIPE every other host (the config-wipe class).
        """
        return cls._from_raw(load_json_safe(registry_path(), default={}))

    @classmethod
    def load_for_update(cls) -> "Registry":
        """Load for a read-modify-write (add / remove).

        Raises :class:`~navig.core.json_io.JsonReadError` if registry.json exists
        but is transiently unreadable, so the caller ABORTS the save instead of
        writing an empty registry over every host. Missing/empty → an empty
        registry (safe); a corrupt file is quarantined as ``registry.json.corrupt``
        and treated as empty.
        """
        return cls._from_raw(load_json_for_update(registry_path(), default={}))

    def save(self) -> None:
        """Persist the registry atomically (temp-file + fsync + atomic replace,
        with transient-lock retry) so a crash mid-write can't truncate it into a
        file the next load reads as empty and then wipes over."""
        atomic_write_json(
            {
                "version": self.version,
                "domains": {name: asdict(dh) for name, dh in self.domains.items()},
            },
            registry_path(),
        )

    # ── ops ─────────────────────────────────────────────────────────────────
    def get(self, domain: str) -> Optional[DevHost]:
        return self.domains.get(domain)

    def put(self, dh: DevHost) -> None:
        self.domains[dh.domain] = dh

    def remove(self, domain: str) -> Optional[DevHost]:
        return self.domains.pop(domain, None)

    def all(self) -> list[DevHost]:
        return sorted(self.domains.values(), key=lambda d: d.ip)
