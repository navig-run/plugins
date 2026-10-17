"""GET /api/deck/social/receipts — the HTTP read side of the receipt ledger.

(Registration-level auth/module gating is the shared requires_module("social")
wrapper tested elsewhere; this pins the handler's filters and shape.)
"""

from __future__ import annotations

import json

import pytest

from navig_social.social.receipts import get_publish_receipts


class FakeRequest:
    def __init__(self, query=None):
        self.query = query or {}
        self.match_info = {}
        self.headers = {}

    async def json(self):
        raise ValueError("no body")


def _payload(resp):
    return json.loads(resp.body)


@pytest.fixture
def ledger():
    return get_publish_receipts()  # conftest: temp NAVIG_DATA_DIR, fresh singleton


async def test_receipts_route_filters(ledger):
    from navig_social.deck_routes import studio as st

    ledger.record(campaign="wave1-launch", network="telegram", ok=True,
                  url="https://t.me/sch8ma/1", content_id="schema:wave1:tx-002",
                  source="studio")
    ledger.record(campaign="wave1-launch", network="youtube", ok=True,
                  url="https://youtu.be/x", content_id="schema:wave1:tx-002",
                  source="upload")
    ledger.record(campaign="other", network="twitter", ok=False, error="nope")

    all_rows = _payload(await st.handle_studio_receipts(FakeRequest()))["data"]["receipts"]
    assert len(all_rows) == 3

    by_campaign = _payload(await st.handle_studio_receipts(
        FakeRequest(query={"campaign": "wave1-launch"})))["data"]["receipts"]
    assert len(by_campaign) == 2

    by_content = _payload(await st.handle_studio_receipts(
        FakeRequest(query={"content_id": "schema:wave1:tx-002"})))["data"]["receipts"]
    assert {r["network"] for r in by_content} == {"telegram", "youtube"}

    by_network = _payload(await st.handle_studio_receipts(
        FakeRequest(query={"network": "youtube"})))["data"]["receipts"]
    assert len(by_network) == 1 and by_network[0]["url"] == "https://youtu.be/x"

    empty = _payload(await st.handle_studio_receipts(
        FakeRequest(query={"content_id": "schema:wave1:none"})))["data"]["receipts"]
    assert empty == []


async def test_receipts_route_shape_carries_identity(ledger):
    from navig_social.deck_routes import studio as st

    ledger.record(campaign="wave1-launch", network="telegram", ok=True,
                  content_id="schema:wave1:tx-002", content_version=2,
                  source="studio", idempotency_key="k1")
    row = _payload(await st.handle_studio_receipts(
        FakeRequest(query={"content_id": "schema:wave1:tx-002"})))["data"]["receipts"][0]
    for key in ("id", "campaign", "network", "target", "post_id", "url", "ok", "error",
                "created_at", "content_id", "content_version", "source", "idempotency_key"):
        assert key in row, f"receipt missing {key}"
    assert row["content_id"] == "schema:wave1:tx-002" and row["source"] == "studio"
