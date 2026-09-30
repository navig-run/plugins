"""Concrete social publishers.

Each ships with credential wiring keyed by the deck's ``vaultProvider`` name.
Where a single token suffices (X, Facebook Page, LinkedIn, Reddit) the publish
call hits the documented API endpoint; the OAuth/app-setup-heavy bits and
platform API limits are surfaced as clear errors rather than silent failures.

All network calls are best-effort and defensive — failures become a
``PublishReceipt`` with an ``error`` rather than raising.
"""

from __future__ import annotations

import logging

from navig_social.social.base import BasePublisher
from navig_social.social.credentials import get_config
from navig_social.social.types import PublishReceipt, RenderedPost

logger = logging.getLogger(__name__)


class TwitterPublisher(BasePublisher):
    name = "twitter"
    char_limit = 280
    # Tweet media needs the separate v1.1 media/upload flow (OAuth 1.0a), which
    # isn't implemented here — so DON'T advertise media support. Advertising it
    # let the composer attach an image that publish() then silently dropped
    # (it only ever sends {"text": …}), reporting a text-only "success". Matches
    # the other text-only publishers (LinkedIn / Facebook / Telegram).
    supports_media = False

    async def publish(self, target: str, post: RenderedPost) -> PublishReceipt:
        if (r := self._require_auth(target)) is not None:
            return r
        try:
            session = await self._session()
            try:
                headers = {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"}
                async with session.post(
                    "https://api.twitter.com/2/tweets", json={"text": post.text[:280]}, headers=headers
                ) as resp:
                    data = await resp.json()
                    tid = (data.get("data") or {}).get("id")
                    # Require the tweet id, not just a 2xx: a degraded 200/201 with
                    # no data.id is NOT a confirmed tweet — report it as a failure
                    # rather than a phantom success (parity with facebook/instagram/
                    # threads/pinterest, which all require the returned id).
                    if resp.status in (200, 201) and tid:
                        return PublishReceipt.success(self.name, target, id=tid,
                                                      url=f"https://x.com/i/web/status/{tid}")
                    return PublishReceipt.failure(self.name, target, _api_err(data))
            finally:
                await session.close()
        except Exception as exc:  # noqa: BLE001
            return PublishReceipt.failure(self.name, target, str(exc))

    async def fetch_metrics(self, post_id: str) -> dict[str, int] | None:
        """Tweet public_metrics → views/likes/reposts/replies (absolute counts)."""
        if not self.is_configured() or not post_id:
            return None
        try:
            session = await self._session()
            try:
                headers = {"Authorization": f"Bearer {self.token()}"}
                url = f"https://api.twitter.com/2/tweets/{post_id}?tweet.fields=public_metrics"
                async with session.get(url, headers=headers) as resp:
                    data = await _safe_json(resp)
                pm = ((data.get("data") or {}).get("public_metrics")) or {}
                if not pm:
                    return None
                return {
                    "views": int(pm.get("impression_count", 0)),
                    "likes": int(pm.get("like_count", 0)),
                    "reposts": int(pm.get("retweet_count", 0)),
                    "replies": int(pm.get("reply_count", 0)),
                }
            finally:
                await session.close()
        except Exception:  # noqa: BLE001 — one network's failure must not break `sync`
            return None


class RedditPublisher(BasePublisher):
    name = "reddit"
    char_limit = None
    supports_media = False

    async def list_targets(self):
        # Reddit posts go to a subreddit; default from config if present.
        from navig_social.social.types import PublishTarget

        sub = get_config("reddit", "subreddit")
        return [PublishTarget(network="reddit", target=sub or "", display=f"r/{sub}" if sub else "reddit (set subreddit)")]

    async def publish(self, target: str, post: RenderedPost) -> PublishReceipt:
        if (r := self._require_auth(target)) is not None:
            return r
        sub = (target or get_config("reddit", "subreddit") or "").lstrip("r/").strip("/")
        if not sub:
            return PublishReceipt.failure(self.name, target, "no subreddit — set one in Settings or the post target")
        title, _, selftext = post.text.partition("\n")
        try:
            session = await self._session()
            try:
                headers = {"Authorization": f"Bearer {self.token()}", "User-Agent": "navig-studio/1.0"}
                form = {"sr": sub, "kind": "self", "title": (title or post.text)[:300], "text": selftext}
                async with session.post("https://oauth.reddit.com/api/submit", data=form, headers=headers) as resp:
                    data = await resp.json()
                    url = (((data or {}).get("json") or {}).get("data") or {}).get("url")
                    if resp.status == 200 and not ((data.get("json") or {}).get("errors")):
                        return PublishReceipt.success(self.name, target, url=url)
                    return PublishReceipt.failure(self.name, target, _api_err(data))
            finally:
                await session.close()
        except Exception as exc:  # noqa: BLE001
            return PublishReceipt.failure(self.name, target, str(exc))


class LinkedInPublisher(BasePublisher):
    name = "linkedin"
    char_limit = 3000
    supports_media = False  # media needs the asset-upload flow — text/link only here

    async def publish(self, target: str, post: RenderedPost) -> PublishReceipt:
        if (r := self._require_auth(target)) is not None:
            return r
        author = target or get_config("linkedin", "author_urn")
        if not author:
            return PublishReceipt.failure(self.name, target, "missing author URN — set linkedin.author_urn in Settings")
        body = {
            "author": author,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": post.text},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        }
        try:
            session = await self._session()
            try:
                headers = {"Authorization": f"Bearer {self.token()}", "X-Restli-Protocol-Version": "2.0.0",
                           "Content-Type": "application/json"}
                async with session.post("https://api.linkedin.com/v2/ugcPosts", json=body, headers=headers) as resp:
                    if resp.status in (200, 201):
                        return PublishReceipt.success(self.name, target, id=resp.headers.get("x-restli-id"))
                    return PublishReceipt.failure(self.name, target, _api_err(await _safe_json(resp)))
            finally:
                await session.close()
        except Exception as exc:  # noqa: BLE001
            return PublishReceipt.failure(self.name, target, str(exc))


class FacebookPagePublisher(BasePublisher):
    name = "facebook"
    char_limit = None
    supports_media = False  # link posts supported; photo upload is a separate endpoint

    async def publish(self, target: str, post: RenderedPost) -> PublishReceipt:
        if (r := self._require_auth(target)) is not None:
            return r
        page_id = target or get_config("facebook", "page_id")
        if not page_id:
            return PublishReceipt.failure(self.name, target, "missing page id — set facebook.page_id in Settings")
        params = {"message": post.text, "access_token": self.token() or ""}
        if post.link:
            params["link"] = post.link
        try:
            session = await self._session()
            try:
                async with session.post(f"https://graph.facebook.com/{page_id}/feed", data=params) as resp:
                    data = await _safe_json(resp)
                    if resp.status == 200 and data.get("id"):
                        return PublishReceipt.success(self.name, target, id=data["id"])
                    return PublishReceipt.failure(self.name, target, _api_err(data))
            finally:
                await session.close()
        except Exception as exc:  # noqa: BLE001
            return PublishReceipt.failure(self.name, target, str(exc))


class InstagramPublisher(BasePublisher):
    name = "instagram"
    char_limit = 2200
    supports_media = True

    async def publish(self, target: str, post: RenderedPost) -> PublishReceipt:
        if (r := self._require_auth(target)) is not None:
            return r
        ig_id = target or get_config("instagram", "ig_user_id")
        image_url = next((m.get("url") for m in post.media if m.get("url")), None)
        if not ig_id:
            return PublishReceipt.failure(self.name, target, "missing IG user id — set instagram.ig_user_id in Settings")
        if not image_url:
            return PublishReceipt.failure(
                self.name, target, "Instagram requires a publicly-reachable image URL (no local upload in this build)"
            )
        token = self.token() or ""
        try:
            session = await self._session()
            try:
                # 1) create media container
                async with session.post(
                    f"https://graph.facebook.com/{ig_id}/media",
                    data={"image_url": image_url, "caption": post.text, "access_token": token},
                ) as resp:
                    cdata = await _safe_json(resp)
                    creation_id = cdata.get("id")
                    if not creation_id:
                        return PublishReceipt.failure(self.name, target, _api_err(cdata))
                # 2) publish container
                async with session.post(
                    f"https://graph.facebook.com/{ig_id}/media_publish",
                    data={"creation_id": creation_id, "access_token": token},
                ) as resp2:
                    pdata = await _safe_json(resp2)
                    if resp2.status == 200 and pdata.get("id"):
                        return PublishReceipt.success(self.name, target, id=pdata["id"])
                    return PublishReceipt.failure(self.name, target, _api_err(pdata))
            finally:
                await session.close()
        except Exception as exc:  # noqa: BLE001
            return PublishReceipt.failure(self.name, target, str(exc))


class YouTubePublisher(BasePublisher):
    name = "youtube"
    char_limit = None
    supports_media = True

    async def publish(self, target: str, post: RenderedPost) -> PublishReceipt:
        if (r := self._require_auth(target)) is not None:
            return r
        # YouTube's public Data API has no community-post endpoint and full video
        # upload is out of scope for this build — surface that clearly.
        return PublishReceipt.failure(
            self.name, target,
            "YouTube text/community posting isn't available via the public API; video upload is out of scope in this build",
        )


class ThreadsPublisher(BasePublisher):
    """Meta Threads — text (or single image) posts via the two-step Graph flow.

    Connect with ``navig social connect threads`` (stores the token at vault
    ``threads`` and the numeric id at ``adapters.social.threads.user_id``).
    """

    name = "threads"
    char_limit = 500
    supports_media = True
    _BASE = "https://graph.threads.net/v1.0"

    async def publish(self, target: str, post: RenderedPost) -> PublishReceipt:
        if (r := self._require_auth(target)) is not None:
            return r
        user = target or get_config("threads", "user_id") or "me"
        token = self.token() or ""
        image_url = _first_image_url(post)
        create = {"text": post.text[:self.char_limit], "access_token": token}
        create["media_type"] = "IMAGE" if image_url else "TEXT"
        if image_url:
            create["image_url"] = image_url
        try:
            session = await self._session()
            try:
                # 1) create a media container
                async with session.post(f"{self._BASE}/{user}/threads", data=create) as resp:
                    cdata = await _safe_json(resp)
                    creation_id = cdata.get("id")
                    if not creation_id:
                        return PublishReceipt.failure(self.name, target, _api_err(cdata))
                # 2) publish it
                async with session.post(
                    f"{self._BASE}/{user}/threads_publish",
                    data={"creation_id": creation_id, "access_token": token},
                ) as resp2:
                    pdata = await _safe_json(resp2)
                    if resp2.status == 200 and pdata.get("id"):
                        return PublishReceipt.success(self.name, target, id=pdata["id"])
                    return PublishReceipt.failure(self.name, target, _api_err(pdata))
            finally:
                await session.close()
        except Exception as exc:  # noqa: BLE001
            return PublishReceipt.failure(self.name, target, str(exc))


class PinterestPublisher(BasePublisher):
    """Pinterest — create a Pin (image + description) on a board.

    A Pin is fundamentally an image, so ``post.media`` must carry a
    publicly-reachable image URL. Board comes from the post target or
    ``adapters.social.pinterest.board_id``.
    """

    name = "pinterest"
    char_limit = 800  # Pin description limit
    supports_media = True

    async def list_targets(self):
        from navig_social.social.types import PublishTarget

        board = get_config("pinterest", "board_id")
        return [PublishTarget(
            network="pinterest", target=board or "",
            display=f"board {board}" if board else "pinterest (set board_id)",
            media=True, char_limit=self.char_limit,
        )]

    async def publish(self, target: str, post: RenderedPost) -> PublishReceipt:
        if (r := self._require_auth(target)) is not None:
            return r
        board_id = target or get_config("pinterest", "board_id")
        if not board_id:
            return PublishReceipt.failure(self.name, target, "missing board — set pinterest.board_id in Settings")
        image_url = _first_image_url(post)
        if not image_url:
            return PublishReceipt.failure(
                self.name, target, "Pinterest requires a publicly-reachable image URL (a Pin is an image)")
        title, body = _split_title(post.text, title_max=100)
        payload = {
            "board_id": board_id,
            "title": title[:100],
            "description": (body or post.text)[:self.char_limit],
            "media_source": {"source_type": "image_url", "url": image_url},
        }
        if post.link:
            payload["link"] = post.link
        try:
            session = await self._session()
            try:
                headers = {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"}
                async with session.post("https://api.pinterest.com/v5/pins", json=payload, headers=headers) as resp:
                    data = await _safe_json(resp)
                    if resp.status in (200, 201) and data.get("id"):
                        pid = data["id"]
                        return PublishReceipt.success(self.name, target, id=pid,
                                                      url=f"https://www.pinterest.com/pin/{pid}/")
                    return PublishReceipt.failure(self.name, target, _api_err(data))
            finally:
                await session.close()
        except Exception as exc:  # noqa: BLE001
            return PublishReceipt.failure(self.name, target, str(exc))


class DevToPublisher(BasePublisher):
    """dev.to — publish a markdown article. The stored API key IS the token.

    The post's first line becomes the title; the rest is the markdown body.
    Auth is the ``api-key`` header (not Bearer).
    """

    name = "devto"
    char_limit = None  # it's a blog article
    supports_media = True  # main_image (cover), optional

    async def publish(self, target: str, post: RenderedPost) -> PublishReceipt:
        if (r := self._require_auth(target)) is not None:
            return r
        title, body = _split_title(post.text, title_max=250)
        if not title:
            return PublishReceipt.failure(self.name, target, "dev.to needs a title (the post's first line)")
        article: dict = {
            "title": title[:250],
            "body_markdown": body or title,
            "published": True,
        }
        # up to 4 tags (alphanumeric); dev.to rejects '#' and spaces in tags.
        tags = [t.lstrip("#").replace(" ", "").lower() for t in getattr(post, "hashtags", []) or []]
        tags = [t for t in tags if t][:4]
        if tags:
            article["tags"] = tags
        image_url = _first_image_url(post)
        if image_url:
            article["main_image"] = image_url
        if post.link:
            article["canonical_url"] = post.link
        try:
            session = await self._session()
            try:
                headers = {"api-key": self.token() or "", "Content-Type": "application/json"}
                async with session.post("https://dev.to/api/articles",
                                        json={"article": article}, headers=headers) as resp:
                    data = await _safe_json(resp)
                    if resp.status in (200, 201) and data.get("url"):
                        return PublishReceipt.success(self.name, target, id=str(data.get("id") or ""),
                                                      url=data["url"])
                    return PublishReceipt.failure(self.name, target, _api_err(data))
            finally:
                await session.close()
        except Exception as exc:  # noqa: BLE001
            return PublishReceipt.failure(self.name, target, str(exc))

    async def fetch_metrics(self, post_id: str) -> dict[str, int] | None:
        """dev.to article stats → views/likes/replies (page_views needs the api-key)."""
        if not self.is_configured() or not post_id:
            return None
        try:
            session = await self._session()
            try:
                headers = {"api-key": self.token() or ""}
                async with session.get(f"https://dev.to/api/articles/{post_id}", headers=headers) as resp:
                    data = await _safe_json(resp)
                if not isinstance(data, dict) or "id" not in data:
                    return None
                return {
                    "views": int(data.get("page_views_count", 0)),
                    "likes": int(data.get("public_reactions_count", 0)),
                    "replies": int(data.get("comments_count", 0)),
                }
            finally:
                await session.close()
        except Exception:  # noqa: BLE001 — one network's failure must not break `sync`
            return None


class TelegramPublisher(BasePublisher):
    """Telegram — post to a chat/channel via the Bot API (standalone, bot-token only).

    Token from vault ``telegram``; target chat id from the post target or
    ``adapters.social.telegram.chat_id``. Distinct from the gateway's live
    ``TelegramChannel`` — this needs only a bot token (no daemon), which is what
    fan-out needs and keeps navig-social installable standalone.
    """

    name = "telegram"
    char_limit = 4096
    supports_media = False  # text posts here; media goes via the live channel / dispatcher

    async def publish(self, target: str, post: RenderedPost) -> PublishReceipt:
        if (r := self._require_auth(target)) is not None:
            return r
        chat_id = target or get_config("telegram", "chat_id")
        if not chat_id:
            return PublishReceipt.failure(self.name, target, "missing chat id — set telegram.chat_id in Settings")
        try:
            session = await self._session()
            try:
                # Render the composed markdown to Telegram HTML — consistent with the
                # gateway fan-out path (this was fragile V1 "Markdown"). Degrades to
                # plain text if core isn't importable (standalone install).
                text = post.text
                parse_mode: str | None = None
                try:
                    from navig.gateway.channels.telegram_html import (
                        MAX_MESSAGE_UTF16,
                        md_to_html,
                        split_html_for_telegram,
                    )

                    html = md_to_html(post.text)
                    chunks = split_html_for_telegram(html, max_utf16=MAX_MESSAGE_UTF16)
                    text = chunks[0] if chunks else html
                    if len(chunks) > 1:
                        logger.warning(
                            "telegram: post exceeds one message — sending the first part only"
                        )
                    parse_mode = "HTML"
                except Exception:  # noqa: BLE001 — no core / convert failed → plain text
                    text = post.text
                    if len(text) > self.char_limit:
                        logger.warning(
                            "telegram: message %d chars exceeds limit %d — truncating",
                            len(text), self.char_limit,
                        )
                        text = text[: self.char_limit]
                url = f"https://api.telegram.org/bot{self.token()}/sendMessage"
                payload = {"chat_id": chat_id, "text": text}
                if parse_mode:
                    payload["parse_mode"] = parse_mode
                async with session.post(url, json=payload) as resp:
                    data = await _safe_json(resp)
                    if resp.status == 200 and data.get("ok"):
                        mid = (data.get("result") or {}).get("message_id")
                        return PublishReceipt.success(self.name, target, id=str(mid) if mid else None)
                    return PublishReceipt.failure(self.name, target, data.get("description") or _api_err(data))
            finally:
                await session.close()
        except Exception as exc:  # noqa: BLE001
            return PublishReceipt.failure(self.name, target, str(exc))


# ── helpers ───────────────────────────────────────────────────


def _first_image_url(post: RenderedPost) -> str | None:
    """First publicly-reachable image URL in the post's media, or None."""
    for m in getattr(post, "media", None) or []:
        url = m.get("url") if isinstance(m, dict) else None
        if url:
            return url
    return None


def _split_title(text: str, *, title_max: int = 100) -> tuple[str, str]:
    """Split *text* into (title, body). Title = first non-empty line (trimmed to
    *title_max*); body = everything after it. If it's a single line longer than
    *title_max*, the whole thing is the title and the body is empty."""
    text = (text or "").strip()
    if not text:
        return "", ""
    first, sep, rest = text.partition("\n")
    first = first.strip()
    if sep:  # multi-line: first line is the title, the remainder is the body
        return first[:title_max], rest.strip()
    if len(first) <= title_max:  # short single line: title only, no body
        return first, ""
    # overlong single line: truncated head is the title, full text is the body
    # (so no content is silently dropped)
    return first[:title_max], text


async def _safe_json(resp) -> dict:
    try:
        return await resp.json()
    except Exception:  # noqa: BLE001
        return {}


def _api_err(data: dict) -> str:
    if not isinstance(data, dict):
        return "API error"
    err = data.get("error")
    if isinstance(err, dict):
        return err.get("message") or err.get("error_description") or str(err)
    if isinstance(err, str):
        return err
    if data.get("errors"):
        return str(data["errors"])
    return str(data) if data else "API error"


# Every built-in publisher class — consumed by the registry.
BUILTIN_PUBLISHERS = [
    TwitterPublisher,
    RedditPublisher,
    LinkedInPublisher,
    FacebookPagePublisher,
    InstagramPublisher,
    YouTubePublisher,
    ThreadsPublisher,
    PinterestPublisher,
    DevToPublisher,
    TelegramPublisher,
]
