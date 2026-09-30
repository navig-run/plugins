"""fan_out — publish ONE brief to many networks in a single call.

Per-platform adaptation + UTM tagging, built on the existing publishing framework
(``PostContent.render`` + ``BasePublisher``). Standalone: resolves each network via
the publisher registry (no gateway needed), so it works with just vault tokens.

The user-supplied brief (arbitrary JSON) is validated into a :class:`Brief` at the
boundary — a typo'd key becomes an error, not a silently-wrong live post.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from navig_social.social.registry import get_publisher_registry
from navig_social.social.types import PostContent, PublishReceipt

DEFAULT_PLATFORMS = ["x", "facebook", "devto", "telegram"]

# brief/CLI platform names → publisher registry names.
_ALIAS = {"x": "twitter"}
# Networks whose link must stay CLEAN (no UTM) — dev.to maps post.link → canonical_url.
_CLEAN_LINK = {"devto"}
_CHAR_LIMIT = {"twitter": 280}


@dataclass
class Brief:
    """A validated fan-out brief. Build with :meth:`from_dict` at the (user JSON) boundary
    so a typo'd key is a hard error rather than a silently-empty live post."""

    title: str = ""
    body: str = ""
    url: str | None = None
    image: str | None = None
    campaign: str | None = None
    per_platform_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Any) -> "Brief":
        if isinstance(d, Brief):
            return d
        if not isinstance(d, dict):
            raise TypeError(f"brief must be a JSON object (got {type(d).__name__})")

        def _opt_str(key: str) -> str | None:
            v = d.get(key)
            if v is None or v == "":
                return None
            if not isinstance(v, str):
                raise ValueError(f"brief field {key!r} must be a string (got {type(v).__name__})")
            return v

        title = (_opt_str("title") or "").strip()
        body = (_opt_str("body") or "").strip()
        if not title and not body:
            raise ValueError("brief needs a non-empty title or body")
        overrides = d.get("per_platform_overrides") or {}
        if not isinstance(overrides, dict):
            raise ValueError("brief field 'per_platform_overrides' must be an object")
        return cls(
            title=title,
            body=body,
            url=_opt_str("url"),
            image=_opt_str("image"),
            campaign=_opt_str("campaign"),
            per_platform_overrides=overrides,
        )


@dataclass(frozen=True)
class PreviewRow:
    """One dry-run preview row (the per-platform adapted payload)."""

    platform: str
    network: str
    text: str
    link: str | None
    chars: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform, "network": self.network,
            "text": self.text, "link": self.link, "chars": self.chars,
        }


def _slug(s: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in (s or "").lower()).strip("-")
    return out[:60] or "post"


def utm(url: str, platform: str, campaign: str) -> str:
    """Append ``utm_source/medium/campaign`` to *url*, preserving any existing query."""
    if not url:
        return url
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q.update({"utm_source": platform, "utm_medium": "social", "utm_campaign": _slug(campaign)})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


def _hook(text: str, limit: int) -> str:
    """First sentence (or trimmed head) that fits under *limit*."""
    text = (text or "").strip().replace("\n", " ")
    first = text.split(". ")[0].strip()
    return (first if len(first) <= limit else text[: max(1, limit)]).rstrip()


def _adapt(platform: str, brief: Brief, campaign: str) -> dict:
    """Build the ``per_network`` override ``{body, link}`` for *platform*."""
    net = _ALIAS.get(platform, platform)  # utm_source = the canonical network name
    title, body = brief.title, brief.body
    raw_url = (brief.url or "").strip()
    over = brief.per_platform_overrides.get(platform, {})
    if "body" in over or "link" in over:
        return over

    if platform in _CLEAN_LINK:
        # dev.to: title (first line) + full markdown; link = CLEAN canonical (no UTM).
        text = f"{title}\n{body}" if title else body
        return {"body": text, "link": raw_url or None}

    link = utm(raw_url, net, campaign) if raw_url else None
    if net == "twitter":
        limit = _CHAR_LIMIT["twitter"]
        room = limit - (len(link) + 1 if link else 0)
        if link and room < 1:
            # A pathologically long link leaves no room for a hook — drop it so the
            # post still fits 280 (rather than emit a 1-char hook + oversized link).
            return {"body": _hook(title or body, limit), "link": None}
        return {"body": _hook(title or body, room), "link": link}
    # facebook / telegram / default: full body + UTM'd link.
    return {"body": body or title, "link": link}


def _build_content(brief: Brief, platforms: list[str], campaign: str) -> tuple[PostContent, dict]:
    media = [{"url": brief.image, "kind": "photo"}] if brief.image else []
    per_network: dict[str, dict] = {}
    net_by_platform: dict[str, str] = {}
    for p in platforms:
        net = _ALIAS.get(p, p)
        net_by_platform[p] = net
        per_network[net] = _adapt(p, brief, campaign)
    content = PostContent(
        body=brief.body, media=media, link=brief.url, per_network=per_network,
    )
    return content, net_by_platform


def _campaign_of(brief: Brief, campaign: str | None) -> str:
    return campaign or brief.campaign or brief.title or "post"


# ── click-tracking redirects (opt-in; auto-closes the measure loop) ──────────


def _tracking_base() -> str | None:
    """Configured public base URL for click-tracking redirects, or None (disabled).

    Reads ``adapters.social.tracking.base_url`` / ``NAVIG_TRACKING_BASE_URL`` —
    typically the lighthouse URL, which forwards ``/r/…`` to the gateway.
    """
    from navig_social.social.credentials import get_config

    base = get_config("tracking", "base_url")
    return base.rstrip("/") if isinstance(base, str) and base.strip() else None


def tracking_link(base: str, campaign_slug: str, network: str) -> str:
    """The public redirect ``<base>/r/<campaign>/<network>`` — records a click, then
    302s to the real destination the receipt ledger holds for that campaign+network."""
    return f"{base.rstrip('/')}/r/{campaign_slug}/{network}"


def _apply_tracking(content: PostContent, campaign_slug: str, base: str) -> dict[str, str]:
    """Rewrite each network's link to a tracking redirect (in place); return the
    original targets by network. dev.to's canonical stays clean (it's an SEO
    canonical, not a click link)."""
    targets: dict[str, str] = {}
    for net, ov in content.per_network.items():
        if net in _CLEAN_LINK:  # devto canonical must stay the real, clean URL
            continue
        orig = ov.get("link")
        if orig:
            targets[net] = orig
            ov["link"] = tracking_link(base, campaign_slug, net)
    return targets


def _resolve_tracking_base(track: bool | None) -> str | None:
    """``track=False`` disables; ``None``/``True`` use the configured base (None if unset)."""
    return None if track is False else _tracking_base()


def preview(brief: Brief | dict, *, platforms=None, campaign=None, track=None) -> list[PreviewRow]:
    """Return the adapted per-platform payloads WITHOUT publishing (dry-run)."""
    brief = Brief.from_dict(brief)
    platforms = platforms if platforms is not None else DEFAULT_PLATFORMS
    campaign = _campaign_of(brief, campaign)
    content, net_by_platform = _build_content(brief, platforms, campaign)
    base = _resolve_tracking_base(track)
    if base:
        _apply_tracking(content, _slug(campaign), base)
    out: list[PreviewRow] = []
    for p in platforms:
        net = net_by_platform[p]
        r = content.render(net)
        out.append(PreviewRow(platform=p, network=net, text=r.text, link=r.link, chars=len(r.text)))
    return out


async def fan_out(
    brief: Brief | dict, *, platforms=None, campaign=None, dry_run=False, record=True, track=None
) -> list[PreviewRow] | list[PublishReceipt]:
    """Publish *brief* to *platforms*.

    Returns preview rows when ``dry_run`` is True, else one ``PublishReceipt`` per
    distinct network (never raises). Only ``platforms=None`` means "use defaults" —
    an explicit empty list publishes to nothing (the CLI guards against that).

    A live publish is **recorded** to the campaign-tagged receipt ledger
    (:mod:`navig_social.social.receipts`) with each network's real UTM'd URL — the
    create→publish→measure loop's durable record. Pass ``record=False`` to skip
    (tests / one-off programmatic calls). Recording is best-effort and never
    affects the returned receipts.

    When ``track`` is on (default: on iff ``adapters.social.tracking.base_url`` is
    set) the published link for each click-bearing network is wrapped in a
    ``/r/<campaign>/<network>`` redirect so clicks auto-populate the engagement
    ledger; the receipt still records the *real* destination (the redirect resolves
    to it). dev.to's canonical stays clean.
    """
    brief = Brief.from_dict(brief)
    platforms = platforms if platforms is not None else DEFAULT_PLATFORMS
    campaign = _campaign_of(brief, campaign)
    if dry_run:
        return preview(brief, platforms=platforms, campaign=campaign, track=track)

    content, net_by_platform = _build_content(brief, platforms, campaign)
    reg = get_publisher_registry()
    slug = _slug(campaign)  # the utm_campaign — the ledger's join key
    base = _resolve_tracking_base(track)
    targets = _apply_tracking(content, slug, base) if base else {}
    receipts: list[PublishReceipt] = []
    rows: list[dict] = []
    seen: set[str] = set()  # dedup: `--to x,twitter` resolves to one network → publish once
    for p in platforms:
        net = net_by_platform[p]
        if net in seen:
            continue
        seen.add(net)
        rendered = content.render(net)
        pub = reg.get(net)
        if pub is None:
            r = PublishReceipt.failure(net, "", f"unknown network '{p}'")
        else:
            try:
                r = await pub.publish("", rendered)
            except Exception as exc:  # noqa: BLE001
                r = PublishReceipt.failure(net, "", str(exc))
        receipts.append(r)
        # Record the REAL destination (targets[net] when the published link was
        # wrapped in a tracking redirect), so the redirect resolves back to it.
        rows.append({
            "campaign": slug, "network": net, "target": r.target,
            "post_id": r.id, "url": getattr(r, "url", None) or targets.get(net) or rendered.link,
            "ok": r.ok, "error": r.error,
        })
    if record and rows:
        from navig_social.social.receipts import record_receipts

        record_receipts(rows)
    return receipts
