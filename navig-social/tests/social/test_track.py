"""G4 — `navig social track <content-id>`: the status-by-content-id envelope."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

import navig_social.social.receipts as receipts_mod
import navig.store.scheduled_posts as sp_mod
from navig.store.scheduled_posts import ScheduledPostStore
from navig_social.commands.social import social_app

runner = CliRunner()

CID = "schema:wave1:2026-10-15-tx-002"


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    sp_store = ScheduledPostStore(db_path=tmp_path / "sp.db")
    pid = sp_store.create(
        body="Null Island.",
        content={"body": "Null Island.", "approval_policy": "required",
                 "content_id": CID},
        targets=[{"network": "telegram", "target": "@sch8ma"}],
        status="scheduled", schedule_kind="once", run_at="2020-01-01T00:00:00Z")
    sp_store.approve(pid, decided_by="owner")
    rstore = receipts_mod.PublishReceiptStore(db_path=tmp_path / "r.db")
    rstore.record(campaign="wave1-launch", network="telegram", target="@sch8ma",
                  post_id="42", url="https://t.me/sch8ma/42", ok=True,
                  content_id=CID, content_version=1, source="studio")
    monkeypatch.setattr(sp_mod, "_store", sp_store)
    monkeypatch.setattr(receipts_mod, "_store", rstore)
    return pid


def test_track_returns_the_contract_envelope(seeded):
    res = runner.invoke(social_app, ["track", CID, "--json"])
    assert res.exit_code == 0, res.output
    env = json.loads(res.output)
    assert env["content_id"] == CID
    assert env["post_id"] == seeded
    assert env["version"]["content_hash"].startswith("sha256:")
    assert env["approval"]["state"] == "approved"
    assert env["networks"][0]["network"] == "telegram"
    assert env["networks"][0]["state"] == "published"
    assert env["networks"][0]["url"] == "https://t.me/sch8ma/42"
    assert env["receipts"][0]["source"] == "studio"
    assert env["source"] == "navig-social-track"


def test_track_unknown_content_exits_1():
    res = runner.invoke(social_app, ["track", "schema:wave1:does-not-exist"])
    assert res.exit_code == 1
