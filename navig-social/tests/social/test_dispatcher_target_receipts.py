"""Every target must yield exactly one PublishReceipt — a malformed target
(blank/missing network) must be a FAILURE receipt, never a silent drop.

The dispatcher's ``publish()`` loop used to ``continue`` past a target whose
network was blank, appending NO receipt. ``run_post`` then derives status from
``ok == len(receipts)``, so a post to ``[telegram, <blank>]`` where telegram
succeeds gave ``receipts=[ok]`` → ``ok == len`` → status **"published"** while the
blank target got nothing and NO error was recorded — silent delivery loss reported
as success (the #622 phantom-success class). Now the blank target yields a failure
receipt, so ``len(receipts) == len(targets)`` (the documented contract) and the
tally can't read "published".
"""
from __future__ import annotations

import pytest

from navig_social.social.dispatcher import PublishDispatcher
from navig_social.social.types import PostContent

pytestmark = pytest.mark.asyncio


class _FakeTG:
    """Minimal live-channel stand-in whose text send always succeeds."""

    _session = None

    async def send_message(self, chat_id, text, **k):
        return {"message_id": 7}


async def test_blank_network_target_yields_failure_receipt_not_dropped():
    disp = PublishDispatcher(telegram_channel=_FakeTG())
    targets = [
        {"network": "telegram", "target": "-100"},
        {"network": "", "target": "-200"},  # malformed — must NOT be silently dropped
    ]

    receipts = await disp.publish(PostContent(body="hi"), targets)

    # The documented contract: exactly one receipt per target.
    assert len(receipts) == len(targets), "a target was silently dropped"
    ok = [r for r in receipts if r.ok]
    bad = [r for r in receipts if not r.ok]
    assert len(ok) == 1 and ok[0].network == "telegram"
    assert len(bad) == 1
    assert "network" in (bad[0].error or "").lower()
    # The exact tally run_post uses must NOT read "published" over a dropped target.
    total_ok = sum(1 for r in receipts if r.ok)
    assert not (receipts and total_ok == len(receipts)), "phantom 'published' over a dropped target"


async def test_missing_network_key_also_fails_not_dropped():
    disp = PublishDispatcher(telegram_channel=_FakeTG())
    receipts = await disp.publish(PostContent(body="hi"), [{"target": "-300"}])  # no 'network' key
    assert len(receipts) == 1
    assert receipts[0].ok is False


async def test_all_valid_targets_still_publish():
    disp = PublishDispatcher(telegram_channel=_FakeTG())
    receipts = await disp.publish(
        PostContent(body="hi"),
        [{"network": "telegram", "target": "-1"}, {"network": "telegram", "target": "-2"}],
    )
    assert len(receipts) == 2
    assert all(r.ok for r in receipts)
