"""The dispatcher's live-TelegramChannel path must send EVERY media item and never
report a text-only "success" when media was dropped.

`_publish_telegram` used to (1) send only ``media[0]`` — silently dropping the rest of
a multi-image post — and (2) fall back to ``send_message(text)`` and return success when
the (single) media item couldn't be resolved (no aiohttp session is ever wired, so any
``url`` attachment resolves to None). Both are silent data loss on Studio's scheduled
fan-out, which reaches this path via ``PublishDispatcher(telegram_channel=...)``.

Now every item is sent, and unresolvable media fails the whole post BEFORE anything is
sent (retry-safe). Under the old code the three "phantom"/"dropped" tests below would
pass with ok=True (verified).
"""

from __future__ import annotations

import pytest

from navig_social.social.dispatcher import PublishDispatcher
from navig_social.social.types import PostContent

pytestmark = pytest.mark.asyncio


class FakeTG:
    """A live-channel stand-in with media send methods, tracking every call."""

    _session = None  # no aiohttp session -> url media can't be fetched

    def __init__(self):
        self.photo_calls: list = []
        self.doc_calls: list = []
        self.text_calls: list = []

    async def send_message(self, chat_id, text, **k):
        self.text_calls.append((chat_id, text))
        return {"message_id": 123}

    async def send_photo(self, chat_id, data, caption=None):
        self.photo_calls.append((chat_id, data, caption))
        return {"message_id": 1}

    async def send_document(self, chat_id, data, filename=None, caption=None):
        self.doc_calls.append((chat_id, data, filename, caption))
        return {"message_id": 2}


def _photo(data=b"\x89PNG\r\n", filename="x.png"):
    return {"data": data, "kind": "photo", "filename": filename}


async def _publish(tg, content):
    disp = PublishDispatcher(telegram_channel=tg)
    receipts = await disp.publish(content, [{"network": "telegram", "target": "-100"}])
    return receipts[0]


async def test_single_photo_delivered_with_text_as_caption():
    tg = FakeTG()
    receipt = await _publish(tg, PostContent(body="Caption text", media=[_photo()]))
    assert receipt.ok is True
    assert receipt.id == "1"
    assert len(tg.photo_calls) == 1
    assert tg.photo_calls[0][2] == "Caption text"  # post text rides as the caption
    assert tg.text_calls == []  # media path, not a text send


async def test_all_media_items_sent_not_just_the_first():
    tg = FakeTG()
    receipt = await _publish(tg, PostContent(body="hi", media=[_photo(), _photo(filename="y.png")]))
    assert receipt.ok is True
    assert len(tg.photo_calls) == 2  # BOTH images sent (old code sent only the first)


async def test_unresolvable_media_fails_not_text_only_success():
    tg = FakeTG()
    receipt = await _publish(
        tg, PostContent(body="New drop!", media=[{"url": "https://cdn.example/i.png", "kind": "photo"}])
    )
    assert receipt.ok is False
    assert tg.photo_calls == []
    assert tg.text_calls == []  # no silent text-only fallback


async def test_partial_unresolvable_fails_before_sending_anything():
    tg = FakeTG()
    receipt = await _publish(
        tg,
        PostContent(body="cap", media=[_photo(), {"url": "https://cdn.example/broken.png", "kind": "photo"}]),
    )
    assert receipt.ok is False
    assert tg.photo_calls == []  # resolved up front -> nothing sent (retry-safe)


async def test_text_only_post_still_succeeds():
    tg = FakeTG()
    receipt = await _publish(tg, PostContent(body="just text"))
    assert receipt.ok is True
    assert receipt.id == "123"
    assert len(tg.text_calls) == 1
    assert tg.photo_calls == []
