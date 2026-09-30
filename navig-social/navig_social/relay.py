"""Client for navig-relay — the scheduled-post queue that lives on Cloudflare.

Telegram bots cannot schedule messages (the Bot API has no method for it, and a bot
trying ``schedule_date`` over MTProto gets ``ScheduleBotNotAllowedError``). So the
schedule lives in the relay Worker (``apps/relay``): D1 holds the queue, a cron
drains it, the bot posts. This module is the operator's side of that — compose,
queue, read back, cancel — so nobody hand-writes JSON into ``curl``.

It speaks the Worker's HTTP API and nothing else. No navig core import: it has to
work under ``pip install navig-social`` alone.

Where the relay is and how to authenticate, first match wins:

    url    --url  ·  $NAVIG_RELAY_URL    ·  vault provider ``relay`` field ``url``
    token         $NAVIG_RELAY_TOKEN  ·  vault provider ``relay`` field ``token``

The token is sent as a bearer header and never printed, logged, or put in an error.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

HTTP_TIMEOUT = 30

#: Telegram's hard limit on a text message, mirrored from the Worker so an oversized
#: post fails before a network round trip, with the length in the message.
TELEGRAM_MAX_CHARS = 4096

_RELATIVE = re.compile(r"^\+\s*(\d+)\s*([mhdw])$", re.IGNORECASE)
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


class RelayError(RuntimeError):
    """A request the relay refused or could not answer. ``status`` is the HTTP code (0 = unreachable)."""

    def __init__(self, message: str, *, status: int = 0, payload: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


# --------------------------------------------------------------------------- configuration

@dataclass(frozen=True)
class RelayConfig:
    url: str
    token: str | None

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)


def _vault(field: str) -> str | None:
    # Reuses the plugin's one vault path, which degrades to None when navig-vault is
    # absent or locked — the standalone install keeps working off the environment.
    from navig_social.stats import _vault_value

    return _vault_value(("relay",), (field,))


def resolve_config(url: str | None = None) -> RelayConfig:
    resolved = (url or os.environ.get("NAVIG_RELAY_URL") or _vault("url") or "").strip().rstrip("/")
    token = os.environ.get("NAVIG_RELAY_TOKEN") or _vault("token")
    return RelayConfig(url=resolved, token=(token or "").strip() or None)


# --------------------------------------------------------------------------- input shaping

def normalize_channel(channel: str) -> str:
    """``miztizm``, ``@miztizm`` and ``t.me/miztizm`` all mean ``@miztizm``."""
    c = channel.strip()
    c = re.sub(r"^(https?://)?(www\.)?t\.me/", "", c, flags=re.IGNORECASE).strip("/")
    if not c:
        raise ValueError("channel is empty")
    return c if c.startswith("@") else f"@{c}"


@dataclass(frozen=True)
class When:
    """A resolved post time, and whether it was relative to the moment the command ran."""

    at: int
    relative: bool


def resolve_when(value: str, *, now: datetime | None = None) -> When:
    """When a post should go out.

    Accepts ``now``, a relative offset (``+30m``, ``+2h``, ``+3d``, ``+1w``), or ISO-8601.
    An ISO time **without** an offset is read in the machine's local timezone — the
    operator types the wall-clock time they mean, and silently treating that as UTC
    would shift every post by the UTC offset.
    """
    now = now or datetime.now(timezone.utc)
    v = value.strip()
    if not v:
        raise ValueError("empty time")
    if v.lower() == "now":
        return When(int(now.timestamp()), relative=True)
    rel = _RELATIVE.match(v)
    if rel:
        delta = timedelta(seconds=int(rel.group(1)) * _UNIT_SECONDS[rel.group(2).lower()])
        return When(int((now + delta).timestamp()), relative=True)
    try:
        parsed = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"can't read {value!r} as a time — use 'now', '+2h', '+3d' or ISO like 2026-10-01T09:00"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()  # local wall-clock → aware
    return When(int(parsed.timestamp()), relative=False)


def parse_when(value: str, *, now: datetime | None = None) -> int:
    """:func:`resolve_when`, as bare unix seconds."""
    return resolve_when(value, now=now).at


def idempotency_key(channel: str, when: When | int, body: str, *, today: str | None = None) -> str:
    """The key that makes re-running the same ``post`` command queue one post, not two.

    The Worker indexes it UNIQUE. What it hashes depends on how the time was given:

    * **An exact time** hashes that time. The same text at two chosen times is two
      posts — a deliberate repeat.
    * **``now`` or ``+2h``** cannot hash the resolved time: it moves on every run, so
      a retry a few seconds later would look like a new post. That is not theoretical
      — it double-posted to a live channel in the end-to-end check. Instead it hashes
      the **day the command ran**: the same text to the same channel, scheduled
      relatively, is queued once per day. ``--force`` is how to say "yes, again".
    """
    if isinstance(when, int):
        when = When(when, relative=False)
    if when.relative:
        # The operator's LOCAL day: "once per day" means their calendar day. A UTC day
        # rolls over at 02:00 in France, splitting one evening's retries across two keys.
        anchor = "rel:" + (today or date.today().isoformat())
    else:
        anchor = f"at:{when.at}"
    digest = hashlib.sha256(f"{channel}\n{anchor}\n{body}".encode("utf-8")).hexdigest()
    return f"cli-{digest[:32]}"


# --------------------------------------------------------------------------- client

class RelayClient:
    def __init__(self, config: RelayConfig, *, timeout: int = HTTP_TIMEOUT) -> None:
        if not config.url:
            raise RelayError("no relay URL configured")
        self.config = config
        self.timeout = timeout

    def _request(self, method: str, path: str, *, body: dict | None = None,
                 query: dict | None = None, auth: bool = True) -> dict:
        url = self.config.url + path
        if query:
            q = {k: v for k, v in query.items() if v not in (None, "")}
            if q:
                url += "?" + urllib.parse.urlencode(q)
        headers = {"content-type": "application/json", "accept": "application/json"}
        if auth:
            if not self.config.token:
                raise RelayError("no relay token configured")
            headers["authorization"] = f"Bearer {self.config.token}"
        req = urllib.request.Request(
            url, method=method, headers=headers,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return _decode(resp.read())
        except urllib.error.HTTPError as exc:
            payload = _decode(exc.read())
            detail = payload.get("error") or payload.get("_text") or exc.reason
            raise RelayError(f"relay said {exc.code}: {detail}", status=exc.code, payload=payload) from None
        except urllib.error.URLError as exc:
            raise RelayError(f"relay unreachable at {self.config.url}: {exc.reason}") from None
        except (OSError, http.client.HTTPException) as exc:
            # urllib wraps only *connection-setup* failures in URLError. A connection that
            # is reset, aborted or hung up while the response is being read escapes as a
            # bare ConnectionResetError / RemoteDisconnected / TimeoutError — measured at
            # ~1 in 75 calls against a local server on Windows. Without this, the CLI
            # prints a traceback. For a POST, the request may still have been applied:
            # enqueue is protected by the idempotency key, and drain by the Worker's claim.
            raise RelayError(
                f"connection to the relay dropped ({type(exc).__name__}) — "
                "the request may or may not have been applied; re-running is safe"
            ) from None

    def health(self) -> dict:
        return self._request("GET", "/health", auth=False)

    def enqueue(self, *, channel: str, body: str, scheduled_at: int, parse_mode: str | None = None,
                disable_preview: bool = False, key: str | None = None) -> dict:
        payload: dict = {"channel": channel, "body": body, "scheduled_at": scheduled_at,
                         "disable_preview": disable_preview}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if key:
            payload["idempotency_key"] = key
        return self._request("POST", "/posts", body=payload)

    def list(self, *, status: str | None = None, channel: str | None = None, limit: int = 100) -> list[dict]:
        out = self._request("GET", "/posts", query={"status": status, "channel": channel, "limit": limit})
        return list(out.get("posts") or [])

    def cancel(self, post_id: str) -> dict:
        return self._request("DELETE", f"/posts/{urllib.parse.quote(post_id, safe='')}")

    def drain(self) -> dict:
        return self._request("POST", "/drain", body={})


def _decode(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {"_value": data}
    except json.JSONDecodeError:
        return {"_text": raw.decode("utf-8", "replace").strip()[:300]}
