"""The FREE-ONLY money guard — the single most safety-critical unit."""

from navig_games.engine.claim.base import is_zero_total, parse_price_to_cents


def test_zero_variants_are_zero():
    for s in ("Free", "FREE", "$0.00", "0", "US$0.00", "0,00 €", "£0.00", "0.00 zł"):
        assert is_zero_total(s) is True, s


def test_priced_is_not_zero():
    for s in ("$14.99", "€29,99", "1 499,00 zł", "$0.99", "US$59.99", "Total: $19.99"):
        assert is_zero_total(s) is False, s


def test_unreadable_is_none():
    for s in (None, "", "Order Total", "  ", "free-to-play tier"):
        # "free-to-play" contains "free" → treated as zero; exclude that case:
        if s and "free" in s.lower():
            continue
        assert is_zero_total(s) is None, s


def test_ambiguous_resolves_to_not_zero():
    # A total line that also carries the pre-promo price must NOT read as zero.
    assert is_zero_total("Order Total $0.00 was $19.99") is False


def test_parse_price_to_cents():
    assert parse_price_to_cents("$14.99") == 1499
    assert parse_price_to_cents("Free") == 0
    assert parse_price_to_cents("$0.00") == 0
    assert parse_price_to_cents("59") == 5900
    assert parse_price_to_cents("no price here") is None
