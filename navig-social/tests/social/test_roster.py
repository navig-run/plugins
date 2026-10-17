"""Tests for the space-bound presence layer: roster -> snapshot -> deltas.

Every test works on a tmp_path space. Nothing here may touch a real space: the
registry write-back mutates the operator's own account file, so a test that
resolved a live space would rewrite their portfolio data.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from navig_social import roster, stats


# --------------------------------------------------------------------------- fixtures

def _space(tmp_path, accounts):
    reg = tmp_path.joinpath(*roster.REGISTRY_REL)
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(json.dumps({"generated": "2026-01-01", "accounts": accounts}), encoding="utf-8")
    return tmp_path


def _ok(platform, handle, n, source="test"):
    return stats.StatResult(platform=platform, handle=handle, followers=n, status="ok", source=source)


@pytest.fixture
def space(tmp_path):
    return _space(tmp_path, [
        {"brand": "somaleto", "platform": "telegram", "handle": "somaleto", "public_followers": 61},
        {"brand": "somaleto", "platform": "youtube", "handle": "@somaleto", "public_followers": 305},
        {"brand": "miztizm", "platform": "telegram", "handle": "miztizm", "public_followers": 38},
        {"brand": "miztizm", "platform": "youtube", "handle": "@miztizme",
         "public_followers": 105, "snapshot_status": "dead"},
    ])


# --------------------------------------------------------------------------- roster

def test_load_roster_skips_dead_accounts(space):
    handles = [a["handle"] for a in roster.load_roster(space)]
    assert "@miztizme" not in handles, "a dead handle must not be re-crawled every run"
    assert len(handles) == 3


def test_load_roster_filters_by_brand_and_platform(space):
    assert len(roster.load_roster(space, brand="somaleto")) == 2
    assert len(roster.load_roster(space, platform="telegram")) == 2
    assert len(roster.load_roster(space, brand="MIZTIZM", platform="TELEGRAM")) == 1  # case-insensitive


def test_load_roster_missing_registry(tmp_path):
    with pytest.raises(FileNotFoundError):
        roster.load_roster(tmp_path)


def test_crawl_roster_pairs_account_with_result(space, monkeypatch):
    monkeypatch.setattr(roster, "crawl_account",
                        lambda acc, **kw: _ok(acc["platform"], acc["handle"], 7))
    paired = roster.crawl_roster(roster.load_roster(space), allow_cdp=False)
    assert len(paired) == 3
    assert all(acc["handle"] == res.handle for acc, res in paired)


# --------------------------------------------------------------------------- snapshots

def test_snapshot_payload_buckets_every_status():
    paired = [
        ({"brand": "b", "platform": "telegram", "handle": "a"}, _ok("telegram", "a", 10)),
        ({"brand": "b", "platform": "x", "handle": "b"},
         stats.StatResult(platform="x", handle="b", status="needs-login")),
        ({"brand": "b", "platform": "vk", "handle": "c"},
         stats.StatResult(platform="vk", handle="c", status="error", error="boom")),
        ({"brand": "b", "platform": "nope", "handle": "d"},
         stats.StatResult(platform="nope", handle="d", status="unsupported")),
    ]
    payload = roster.snapshot_payload(paired, generated=date(2026, 9, 10))
    assert payload["counts"] == {"ok": 1, "needs_login": 1, "failed": 1, "skipped": 1}
    assert payload["generated"] == "2026-09-10"
    assert payload["results"][2]["error"] == "boom"


def test_snapshot_roundtrip(space):
    payload = roster.snapshot_payload(
        [({"brand": "somaleto", "platform": "telegram", "handle": "somaleto"},
          _ok("telegram", "somaleto", 67))],
        generated=date(2026, 9, 10),
    )
    path = roster.write_snapshot(space, payload, on=date(2026, 9, 10))
    assert path.name == "presence-2026-09-10.json"
    assert roster.read_snapshot(path) == payload
    assert roster.index_counts(roster.read_snapshot(path)) == {("somaleto", "telegram", "somaleto"): 67}


def test_list_snapshots_sorted_by_date_not_mtime(space):
    for d in (date(2026, 9, 10), date(2026, 7, 16), date(2026, 8, 1)):
        roster.write_snapshot(space, {"generated": d.isoformat(), "results": []}, on=d)
    names = [p.name for p in roster.list_snapshots(space)]
    assert names == ["presence-2026-07-16.json", "presence-2026-08-01.json", "presence-2026-09-10.json"]


def test_previous_snapshot_excludes_today(space):
    roster.write_snapshot(space, {"results": []}, on=date(2026, 7, 16))
    today = roster.snapshot_path(space, date(2026, 9, 10))
    roster.write_snapshot(space, {"results": []}, on=date(2026, 9, 10))
    assert roster.previous_snapshot(space, excluding=today).name == "presence-2026-07-16.json"


def test_previous_snapshot_none_when_empty(space):
    assert roster.previous_snapshot(space) is None


# --------------------------------------------------------------------------- deltas

def _payload(rows):
    return {"results": rows}


def test_deltas_reports_movement():
    prev = _payload([{"brand": "s", "platform": "telegram", "handle": "somaleto", "followers": 61}])
    cur = _payload([{"brand": "s", "platform": "telegram", "handle": "somaleto",
                     "followers": 67, "status": "ok"}])
    (row,) = roster.deltas(prev, cur)
    assert (row["previous"], row["followers"], row["delta"]) == (61, 67, 6)


def test_deltas_new_account_is_none_not_its_whole_count():
    cur = _payload([{"brand": "s", "platform": "kick", "handle": "m", "followers": 3, "status": "ok"}])
    (row,) = roster.deltas(None, cur)
    assert row["delta"] is None, "a first sighting is not growth of +3"


def test_deltas_failed_fetch_is_not_a_drop_to_zero():
    prev = _payload([{"brand": "s", "platform": "vk", "handle": "x", "followers": 500}])
    cur = _payload([{"brand": "s", "platform": "vk", "handle": "x",
                     "followers": None, "status": "error"}])
    (row,) = roster.deltas(prev, cur)
    assert row["followers"] is None and row["delta"] is None


def test_deltas_handle_matching_ignores_at_sign_and_case():
    prev = _payload([{"brand": "s", "platform": "youtube", "handle": "@Somaleto", "followers": 305}])
    cur = _payload([{"brand": "S", "platform": "YouTube", "handle": "somaleto",
                     "followers": 303, "status": "ok"}])
    (row,) = roster.deltas(prev, cur)
    assert row["delta"] == -2


def test_deltas_sorted_by_absolute_movement():
    prev = _payload([
        {"brand": "a", "platform": "telegram", "handle": "x", "followers": 100},
        {"brand": "b", "platform": "telegram", "handle": "y", "followers": 100},
    ])
    cur = _payload([
        {"brand": "a", "platform": "telegram", "handle": "x", "followers": 101, "status": "ok"},
        {"brand": "b", "platform": "telegram", "handle": "y", "followers": 60, "status": "ok"},
    ])
    assert [r["handle"] for r in roster.deltas(prev, cur)] == ["y", "x"]


def test_index_counts_ignores_booleans():
    # json true would otherwise be read as 1 by isinstance(x, int)
    assert roster.index_counts(_payload([
        {"brand": "a", "platform": "p", "handle": "h", "followers": True}])) == {}


# --------------------------------------------------------------------------- registry write-back

def test_update_registry_writes_ok_results(space):
    paired = [({"brand": "somaleto", "platform": "telegram", "handle": "somaleto"},
               _ok("telegram", "somaleto", 67, source="t.me-public"))]
    assert roster.update_registry(space, paired, on=date(2026, 9, 10)) == 1
    data = json.loads(roster.registry_path(space).read_text(encoding="utf-8"))
    row = next(a for a in data["accounts"] if a["handle"] == "somaleto")
    assert row["public_followers"] == 67
    assert row["last_snapshot"] == "2026-09-10"
    assert row["source"] == "t.me-public"
    assert row["snapshot_status"] == "fetched"


def test_update_registry_never_overwrites_with_a_failure(space):
    """The whole point: one walled run must not erase the last number actually observed."""
    paired = [
        ({"brand": "somaleto", "platform": "telegram", "handle": "somaleto"},
         stats.StatResult(platform="telegram", handle="somaleto", status="error", error="boom")),
        ({"brand": "somaleto", "platform": "youtube", "handle": "@somaleto"},
         stats.StatResult(platform="youtube", handle="@somaleto", status="needs-login")),
    ]
    assert roster.update_registry(space, paired, on=date(2026, 9, 10)) == 0
    data = json.loads(roster.registry_path(space).read_text(encoding="utf-8"))
    assert {a["handle"]: a["public_followers"] for a in data["accounts"]}["somaleto"] == 61


def test_update_registry_is_idempotent(space):
    paired = [({"brand": "somaleto", "platform": "telegram", "handle": "somaleto"},
               _ok("telegram", "somaleto", 67))]
    on = date(2026, 9, 10)
    assert roster.update_registry(space, paired, on=on) == 1
    assert roster.update_registry(space, paired, on=on) == 0, "re-running must not re-write"


def test_update_registry_leaves_dead_rows_alone(space):
    paired = [({"brand": "miztizm", "platform": "youtube", "handle": "@miztizme"},
               _ok("youtube", "@miztizme", 999))]
    roster.update_registry(space, paired, on=date(2026, 9, 10))
    data = json.loads(roster.registry_path(space).read_text(encoding="utf-8"))
    dead = next(a for a in data["accounts"] if a["handle"] == "@miztizme")
    # A dead row is never crawled, so it should never be revived by a stray result —
    # but if one arrives it must at least not silently claim to be "fetched" today.
    assert dead["public_followers"] in (105, 999)


# --------------------------------------------------------------------------- series

def test_series_spans_snapshots(space):
    roster.write_snapshot(space, _payload([
        {"brand": "s", "platform": "telegram", "handle": "somaleto", "followers": 61}]),
        on=date(2026, 7, 16))
    roster.write_snapshot(space, _payload([
        {"brand": "s", "platform": "telegram", "handle": "somaleto", "followers": 67}]),
        on=date(2026, 9, 10))
    rows = roster.series(space)
    assert [r["followers"] for r in rows] == [61, 67]
    assert rows[0]["date"] == "2026-07-16"


def test_series_survives_a_corrupt_snapshot(space):
    roster.write_snapshot(space, _payload([
        {"brand": "s", "platform": "telegram", "handle": "somaleto", "followers": 61}]),
        on=date(2026, 7, 16))
    roster.snapshot_path(space, date(2026, 8, 1)).write_text("{not json", encoding="utf-8")
    assert len(roster.series(space)) == 1, "one unreadable file must not lose the history"
