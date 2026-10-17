"""Claim result type + the money guard that enforces the FREE-ONLY invariant."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Terminal statuses for a claim attempt.
STATUS_CLAIMED = "claimed"  # $0 checkout completed
STATUS_OWNED = "already_owned"  # already in the library, nothing to do
STATUS_MANUAL = "needs_manual"  # captcha / 2FA / login / DOM drift — handed to the user
STATUS_GRABBED = "grabbed"  # you got it yourself (the only terminal state a
#                             non-Epic giveaway can ever reach — we never claim those)
STATUS_PRICED = "skipped_priced"  # a real price was seen — refused (never buys)
STATUS_DRYRUN = "dry_run"  # verified free & stopped before placing the order
STATUS_FAILED = "failed"  # unexpected error

_SUCCESS = {STATUS_CLAIMED, STATUS_OWNED, STATUS_DRYRUN}


@dataclass
class ClaimResult:
    game_key: str
    title: str
    status: str
    store: str = "epic"
    message: str = ""
    screenshot: str = ""
    url: str = ""

    @property
    def ok(self) -> bool:
        return self.status in _SUCCESS

    def to_dict(self) -> dict:
        from dataclasses import asdict

        d = asdict(self)
        d["ok"] = self.ok
        return d


def is_zero_total(text: str | None) -> bool | None:
    """Is the given *total* string exactly zero?

    Returns True for "Free" / "$0.00" / "0,00 €", False if any non-zero digit is
    present (i.e. a real cost), and None if no amount can be read.

    Pass the isolated **order-total** substring — not a whole overlay body, which
    may also contain the pre-promo price. Ambiguity resolves toward *not* zero
    (False), which the claim engine treats as "refuse", so a parse error can only
    ever *prevent* a purchase, never cause one.
    """
    if text is None:
        return None
    t = str(text).strip().lower()
    if not t:
        return None
    if "free" in t:
        return True
    digits = re.findall(r"\d", t)
    if not digits:
        return None
    return all(d == "0" for d in digits)


def parse_price_to_cents(text: str | None) -> int | None:
    """Best-effort display-price → integer cents (informational / tests).

    Handles ``$14.99`` → 1499 and ``Free`` → 0. Returns None when no amount is
    found. Not used as the safety gate — :func:`is_zero_total` is.
    """
    if text is None:
        return None
    t = str(text).strip().lower()
    if not t:
        return None
    if "free" in t:
        return 0
    # Grab the first monetary token: an integer part with optional 2-digit decimal.
    m = re.search(r"(\d[\d,\s]*)(?:[.,](\d{2}))?(?!\d)", t)
    if not m:
        return None
    whole = re.sub(r"[,\s]", "", m.group(1))
    frac = m.group(2) or "00"
    try:
        return int(whole) * 100 + int(frac)
    except ValueError:
        return None
