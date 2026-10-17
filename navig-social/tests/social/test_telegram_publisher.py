"""TelegramPublisher — sendMessage (mocked) + requires_auth without a token."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from navig_social.social.publishers import TelegramPublisher
from navig_social.social.types import RenderedPost


def _post() -> RenderedPost:
    return RenderedPost(text="hello world\nhttps://cybesis.com/x", media=[], link="https://cybesis.com/x")


class _Resp:
    status = 200

    async def json(self):
        return {"ok": True, "result": {"message_id": 42}}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


class _Session:
    def __init__(self):
        self.sent = None

    def post(self, url, json=None):
        self.sent = {"url": url, "json": json}
        return _Resp()

    async def close(self):
        pass


async def test_requires_auth_without_token():
    pub = TelegramPublisher()
    with patch.object(TelegramPublisher, "token", return_value=None):
        r = await pub.publish("123", _post())
    assert not r.ok and r.requires_auth


async def test_publish_posts_to_sendmessage():
    pub = TelegramPublisher()
    session = _Session()
    with patch.object(TelegramPublisher, "token", return_value="bot-token"), \
         patch.object(TelegramPublisher, "_session", new=AsyncMock(return_value=session)):
        r = await pub.publish("123", _post())  # explicit chat id → no get_config needed
    assert r.ok and r.id == "42"
    assert session.sent["json"]["chat_id"] == "123"
    assert "api.telegram.org/botbot-token/sendMessage" in session.sent["url"]
