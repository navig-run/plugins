"""G5/G3 — idempotent publish paths: fan-out skip, upload skip, unique ledger."""
from __future__ import annotations

import asyncio

import pytest
from typer.testing import CliRunner

import navig_social.social.fanout as fanout_mod
import navig_social.social.receipts as receipts_mod
from navig_social.commands.social import social_app
from navig_social.social.types import PublishReceipt

runner = CliRunner()


class _FakePub:
    name = "telegram"

    def __init__(self):
        self.calls = 0

    async def publish(self, text, target=""):
        self.calls += 1
        return PublishReceipt.success("telegram", "@sch8ma", id=f"m{self.calls}",
                                      url=f"https://t.me/x/{self.calls}")


@pytest.fixture()
def rstore(tmp_path):
    """A ledger on a tmp file (no singleton patch — this test records directly)."""
    return receipts_mod.PublishReceiptStore(db_path=tmp_path / "r-direct.db")


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Both singletons on tmp files; registry returns one fake publisher."""
    pub = _FakePub()
    rstore = receipts_mod.PublishReceiptStore(db_path=tmp_path / "r.db")
    monkeypatch.setattr(receipts_mod, "_store", rstore)
    monkeypatch.setattr(fanout_mod, "get_publisher_registry",
                        lambda: {"telegram": pub})
    return pub, rstore


def test_fanout_with_content_id_publishes_once_per_network(isolated):
    pub, rstore = isolated
    brief = {"title": "t", "body": "hello world"}
    cid = "schema:wave1:2026-10-15-tx-002"

    first = asyncio.run(fanout_mod.fan_out(
        brief, platforms=["telegram"], content_id=cid, content_version=1))
    assert len(first) == 1 and first[0].ok and pub.calls == 1
    rows = rstore.list(content_id=cid)
    assert len(rows) == 1
    assert rows[0]["source"] == "fanout"
    assert rows[0]["idempotency_key"] == f"{cid}:telegram"
    assert rows[0]["content_version"] == 1

    # replay: same content_id → prior receipt returned, NOTHING published
    second = asyncio.run(fanout_mod.fan_out(
        brief, platforms=["telegram"], content_id=cid, content_version=1))
    assert len(second) == 1 and second[0].ok
    assert second[0].id == "m1" and second[0].url == "https://t.me/x/1"
    assert pub.calls == 1, "replayed fan-out must not publish again"
    assert len(rstore.list(content_id=cid)) == 1, "no duplicate ledger row"

    # --force overrides
    third = asyncio.run(fanout_mod.fan_out(
        brief, platforms=["telegram"], content_id=cid, content_version=1, force=True))
    assert pub.calls == 2 and third[0].id == "m2"


def test_receipt_idempotency_key_never_double_records(rstore):
    row = {"campaign": "c", "network": "youtube", "post_id": "v1",
           "url": "u", "ok": True, "source": "upload",
           "content_id": "schema:wave1:weekly:x", "content_version": 1,
           "idempotency_key": "schema:wave1:weekly:x:youtube"}
    assert rstore.record(**row) > 0
    assert rstore.record(**row) == 0, "duplicate idempotency_key must not insert"
    assert len(rstore.list(content_id="schema:wave1:weekly:x")) == 1


def test_upload_dedupes_by_content_id_without_calling_upload(tmp_path, monkeypatch):
    rstore = receipts_mod.PublishReceiptStore(db_path=tmp_path / "r2.db")
    monkeypatch.setattr(receipts_mod, "_store", rstore)
    rstore.record(campaign="", network="youtube", post_id="vid9",
                  url="https://youtu.be/vid9", ok=True, source="upload",
                  content_id="schema:wave1:weekly:2026-10-15-door",
                  idempotency_key="schema:wave1:weekly:2026-10-15-door:youtube")

    import navig_social.social.upload as up_mod

    def _boom(*a, **k):  # the engine must never be reached
        raise AssertionError("upload engine called despite an existing receipt")

    monkeypatch.setattr(up_mod, "upload", _boom)
    monkeypatch.setattr("navig_social.social.accounts.resolve_account",
                        lambda *a, **k: "chan1")
    tiny = tmp_path / "v.mp4"
    tiny.write_bytes(b"\x00\x00\x00\x18ftypmp42")

    res = runner.invoke(social_app, [
        "upload", str(tiny), "--title", "t", "--content-id",
        "schema:wave1:weekly:2026-10-15-door", "--json"])
    assert res.exit_code == 0, res.output
    payload = __import__("json").loads(res.output)
    assert payload.get("already_published") is True
    assert payload.get("video_id") == "vid9"
