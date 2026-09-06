"""PublishDispatcher — fan a composed post out to many targets.

Routing:
- ``telegram`` targets go straight to the live ``TelegramChannel`` (text via
  ``send_message``, media via ``send_photo``/``send_video``/``send_document``),
  reusing the existing send path.
- other messaging networks (``discord``/``whatsapp``/``sms``) go through the
  messaging adapter registry's ``send_message(thread_id, text, attachments)``.
- everything else is a :class:`SocialPublisher` from the publisher registry.

Each target yields one :class:`PublishReceipt`; the dispatcher never raises.
"""

from __future__ import annotations

import logging
from typing import Any

from navig_social.social.types import PostContent, PublishReceipt

logger = logging.getLogger(__name__)

MESSAGING_NETWORKS = {"telegram", "discord", "whatsapp", "sms"}


class PublishDispatcher:
    def __init__(
        self,
        *,
        telegram_channel: Any | None = None,
        adapter_registry: Any | None = None,
        publisher_registry: Any | None = None,
    ) -> None:
        self._tg = telegram_channel
        self._adapters = adapter_registry
        self._publishers = publisher_registry

    def _adapter_reg(self):
        if self._adapters is None:
            from navig.messaging.adapter_registry import get_adapter_registry

            self._adapters = get_adapter_registry()
        return self._adapters

    def _publisher_reg(self):
        if self._publishers is None:
            from navig_social.social.registry import get_publisher_registry

            self._publishers = get_publisher_registry()
        return self._publishers

    async def publish(self, content: PostContent, targets: list[dict[str, Any]]) -> list[PublishReceipt]:
        """Publish *content* to each ``{network, target}`` in *targets*."""
        receipts: list[PublishReceipt] = []
        for t in targets:
            network = (t.get("network") or "").strip()
            addr = (t.get("target") or "").strip()
            if not network:
                # A target with no network is malformed — record it as a FAILED
                # receipt, never silently `continue`. The loop must yield exactly
                # one receipt per target (see the class docstring); dropping this
                # one lets run_post's `ok == len(receipts)` mark the post
                # "published" while this target got nothing and no error was
                # recorded — silent delivery loss reported as success.
                receipts.append(
                    PublishReceipt.failure(network or "?", addr, "target has no network")
                )
                continue
            try:
                receipts.append(await self._publish_one(network, addr, content))
            except Exception as exc:  # noqa: BLE001
                logger.exception("publish to %s failed", network)
                receipts.append(PublishReceipt.failure(network, addr, str(exc)))
        return receipts

    async def _publish_one(self, network: str, addr: str, content: PostContent) -> PublishReceipt:
        rendered = content.render(network)
        if network == "telegram" and self._tg is not None:
            return await self._publish_telegram(addr, rendered)
        if network in MESSAGING_NETWORKS:
            return await self._publish_messaging(network, addr, rendered)
        publisher = self._publisher_reg().get(network)
        if publisher is None:
            return PublishReceipt.failure(network, addr, f"unknown network '{network}'")
        return await publisher.publish(addr, rendered)

    async def _publish_telegram(self, addr: str, rendered) -> PublishReceipt:
        try:
            chat_id = int(addr)
        except (TypeError, ValueError):
            return PublishReceipt.failure("telegram", addr, "target must be a chat id")
        bot = self._tg
        try:
            media = rendered.media or []
            if not media:
                msg = await bot.send_message(chat_id, rendered.text)
                return PublishReceipt.success("telegram", addr, id=_tg_message_id(msg))

            # Send EVERY media item (the old code sent only media[0], silently dropping
            # the rest) and resolve every item's bytes UP FRONT — resolution is
            # side-effect-free, so a url that can't be fetched fails the whole post
            # BEFORE anything is sent, instead of silently falling back to a text-only
            # "success" that lost the media. Resolving first keeps the common failure
            # retry-safe: nothing went out, so a retry can't duplicate.
            session = getattr(bot, "_session", None)
            resolved: list[tuple[dict[str, Any], bytes]] = []
            for m in media:
                data = await _media_bytes(m, session)
                if data is None:
                    return PublishReceipt.failure(
                        "telegram", addr, f"could not resolve media {m.get('filename') or '?'}"
                    )
                resolved.append((m, data))

            # The post text rides as the caption of the first item (Telegram caps
            # captions at 1024); later items keep their own caption, if any.
            first_id: str | None = None
            for i, (m, data) in enumerate(resolved):
                caption = (rendered.text[:1024] or None) if i == 0 else (m.get("caption") or None)
                mid = await self._send_media_item(bot, chat_id, m, data, caption)
                if mid is None:
                    return PublishReceipt.failure(
                        "telegram", addr, f"telegram could not send media {m.get('filename') or '?'}"
                    )
                if first_id is None:
                    first_id = mid
            return PublishReceipt.success("telegram", addr, id=first_id)
        except Exception as exc:  # noqa: BLE001
            return PublishReceipt.failure("telegram", addr, str(exc))

    async def _send_media_item(self, bot, chat_id: int, m: dict[str, Any], data: bytes,
                               caption: str | None):
        """Dispatch one already-resolved media item via the live channel's send methods.

        Returns the message id, or ``None`` when no send method is available for it.
        """
        kind = (m.get("kind") or "").lower()
        if kind == "photo" and hasattr(bot, "send_photo"):
            msg = await bot.send_photo(chat_id, data, caption=caption)
        elif kind == "video" and hasattr(bot, "send_video"):
            msg = await bot.send_video(chat_id, data, caption=caption)
        elif hasattr(bot, "send_document"):
            msg = await bot.send_document(chat_id, data, filename=m.get("filename") or "file",
                                          caption=caption)
        else:
            return None
        return _tg_message_id(msg)

    async def _publish_messaging(self, network: str, addr: str, rendered) -> PublishReceipt:
        adapter = self._adapter_reg().get(network)
        if adapter is None:
            return PublishReceipt.failure(network, addr, f"{network} adapter not registered")
        try:
            receipt = await adapter.send_message(addr, rendered.text, rendered.media or None)
            if getattr(receipt, "ok", False):
                return PublishReceipt.success(network, addr, id=getattr(receipt, "message_id", None))
            return PublishReceipt.failure(network, addr, getattr(receipt, "error", None) or "send failed")
        except Exception as exc:  # noqa: BLE001
            return PublishReceipt.failure(network, addr, str(exc))


def _tg_message_id(msg: Any) -> str | None:
    """Extract a message id from a dict (channel) or object (PTB Message), as a str."""
    mid = msg.get("message_id") if isinstance(msg, dict) else getattr(msg, "message_id", None)
    return str(mid) if mid else None


async def _media_bytes(att: dict[str, Any], session: Any) -> bytes | None:
    from navig.messaging.attachments import attachment_bytes

    return await attachment_bytes(att, session)
