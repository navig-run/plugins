"""SocialPublisher protocol + a shared base class for platform publishers."""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from navig_social.social.credentials import get_token
from navig_social.social.types import PublishReceipt, PublishTarget, RenderedPost

logger = logging.getLogger(__name__)


@runtime_checkable
class SocialPublisher(Protocol):
    """One-to-many publish surface for a social network."""

    name: str

    @property
    def capabilities(self) -> dict[str, Any]: ...

    def is_configured(self) -> bool: ...

    async def list_targets(self) -> list[PublishTarget]: ...

    async def publish(self, target: str, post: RenderedPost) -> PublishReceipt: ...


class BasePublisher:
    """Common publisher behaviour: credential lookup + capability defaults.

    Subclasses set ``name`` and override :meth:`publish`. Many platforms in this
    build ship as credential-wired stubs: if no token is configured, ``publish``
    short-circuits to a ``requires_auth`` receipt; once the user supplies a token
    via Settings the concrete API call runs.
    """

    name: str = "social"
    # Optional per-class brand override. Normally leave empty — the canonical
    # casing lives in ``navig_social.social.labels.DISPLAY_LABELS``; set this
    # only for a label not derivable from ``name``. See ``display_label``.
    label: str = ""
    char_limit: int | None = None
    supports_media: bool = True
    # Networks that can hold several connected accounts (see social/accounts.py). For
    # them a target of "@<id>" means "publish as account <id>". Off by default: on
    # Telegram a target like "@channel" is a destination, not an account.
    multi_account: bool = False

    @property
    def display_label(self) -> str:
        """The brand-cased name to show a human — an explicit ``label`` if set,
        else the canonical mapping for ``name`` (``labels.display_label``:
        ``"linkedin"`` → ``"LinkedIn"``, ``"reddit"`` → ``"Reddit"``)."""
        from navig_social.social.labels import display_label as brand_label
        return self.label or brand_label(self.name)

    @property
    def capabilities(self) -> dict[str, Any]:
        return {"text": True, "media": self.supports_media, "char_limit": self.char_limit}

    def token(self) -> str | None:
        return get_token(self.name)

    def account_token(self, account: str | None = None) -> str | None:
        """One connected account's token; the default account goes through ``token()``,
        so a subclass or test that overrides ``token()`` keeps working."""
        from navig_social.social.accounts import is_default

        if is_default(self.name, account):
            return self.token()
        return get_token(self.name, account)

    def is_configured(self, account: str | None = None) -> bool:
        return bool(self.account_token(account))

    def setting(self, key: str, account: str | None = None) -> Any:
        """A per-account setting (page id, author URN); the default account's when None."""
        from navig_social.social.accounts import account_setting

        return account_setting(self.name, key, account)

    def _with_account(self, target: str) -> tuple[str | None, str]:
        """``(account, destination)`` for a post target — see ``multi_account``."""
        if not self.multi_account:
            return None, target
        from navig_social.social.accounts import split_target

        return split_target(target)

    async def list_targets(self) -> list[PublishTarget]:
        # Several connected accounts → one target each ("" stays the default account).
        if self.multi_account:
            from navig_social.social.accounts import list_accounts

            accounts = list_accounts(self.name)
            if len(accounts) > 1:
                return [
                    PublishTarget(
                        network=self.name,
                        target="" if a["default"] else f"@{a['id']}",
                        display=a["label"] or a["id"] or f"{self.name} (default account)",
                        media=self.supports_media,
                        char_limit=self.char_limit,
                    )
                    for a in accounts
                ]
        # Default: a single "account" target.
        return [
            PublishTarget(
                network=self.name,
                target="",
                display=f"{self.name} (default account)",
                media=self.supports_media,
                char_limit=self.char_limit,
            )
        ]

    async def publish(self, target: str, post: RenderedPost) -> PublishReceipt:  # pragma: no cover - overridden
        raise NotImplementedError

    async def fetch_metrics(self, post_id: str) -> dict[str, int] | None:
        """Live engagement for a published post — ``{"views": N, "likes": N, …}`` of
        **absolute** counts, or ``None`` when this network exposes no read API.

        Overridden by networks with a public-metrics endpoint (twitter, dev.to).
        ``navig social sync`` calls this and upserts the result so the report's CTR
        is real without manual entry. Default: unsupported.
        """
        return None

    # ── helpers for subclasses ────────────────────────────────

    def _require_auth(self, target: str, account: str | None = None) -> PublishReceipt | None:
        if not self.is_configured(account):
            return PublishReceipt.failure(
                self.name, target,
                f"{self.name} not connected — add a token in Settings",
                requires_auth=True,
            )
        return None

    async def _session(self):
        import aiohttp

        return aiohttp.ClientSession()
