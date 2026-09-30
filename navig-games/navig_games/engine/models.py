"""Shared data types for the games engine."""

from __future__ import annotations

from dataclasses import asdict, dataclass

#: A free game with this many whole days (or fewer) left counts as "ending soon" —
#: the threshold shared by the `check` view (red countdown + ⏰ line) and the
#: scheduled "grab it before it's gone" reminder, so they can't drift apart.
ENDING_SOON_DAYS = 2


def days_until(iso: "str | None", *, now=None) -> "int | None":
    """Whole days from now until an ISO end-time — 0 = today/imminent, 1 = tomorrow.

    None when there's no end date, it's open-ended, or it can't be parsed.
    Day-granularity is deliberate: the community giveaway feeds don't carry a
    reliable timezone, so hour-level precision would be false confidence. A
    past/negative delta clamps to 0 ("ending now"), never a negative count. Pass
    ``now`` (a tz-aware datetime) to make it deterministic in tests.
    """
    if not iso:
        return None
    from datetime import datetime, timezone

    try:
        end = datetime.fromisoformat(str(iso).replace("Z", "+00:00").replace(" ", "T"))
    except (ValueError, TypeError):
        return None
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    ref = now or datetime.now(timezone.utc)
    return max(0, (end - ref).days)


@dataclass
class FreeGame:
    """A store promotion. ``is_current`` free games are claimable now; otherwise
    it is an upcoming freebie.

    ``original_price_cents`` is the *pre-promotion* price (informational). During
    an active free promo the price the user pays is 0 — that is what the claim
    engine independently re-verifies at the checkout overlay before confirming.
    """

    store: str
    title: str
    offer_id: str = ""
    namespace: str = ""
    slug: str = ""
    url: str = ""
    description: str = ""
    #: Human-readable steps to actually get it (giveaway feeds supply these).
    #: Empty for stores we claim/link directly (Epic, Steam).
    instructions: str = ""
    seller: str = ""
    image: str = ""
    original_price: str = ""  # display string, e.g. "$14.99"
    original_price_cents: int = 0
    currency: str = "USD"
    starts_at: str = ""  # ISO-8601 UTC
    ends_at: str = ""  # ISO-8601 UTC
    is_current: bool = True

    @property
    def key(self) -> str:
        """Stable identity for the ledger (store-scoped)."""
        ident = self.offer_id or self.slug or self.title
        return f"{self.store}:{ident}".lower()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["key"] = self.key
        return d
