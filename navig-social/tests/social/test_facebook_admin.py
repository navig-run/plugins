"""Unit tests for the Facebook Page admin pure helpers.

Focus is the *safety-critical* path: ``plan_deletions`` must never mark a photo
deletable unless a verified, non-empty local backup of it exists. These helpers
have no navig imports, so they run without a daemon or network.
"""
from __future__ import annotations

from navig_social.social.facebook_admin import best_image, ext_from_url, plan_deletions


def test_best_image_picks_largest_variant():
    item = {"images": [
        {"source": "small", "width": 100, "height": 100},
        {"source": "big", "width": 800, "height": 600},
        {"source": "mid", "width": 400, "height": 300},
    ]}
    assert best_image(item)["source"] == "big"


def test_best_image_none_when_empty():
    assert best_image({"images": []}) is None
    assert best_image({}) is None


def test_ext_from_url_handles_query_and_missing():
    assert ext_from_url("https://cdn/x/123.jpg?oh=abc&oe=DEF") == ".jpg"
    assert ext_from_url("https://cdn/x/123.png?token=1") == ".png"
    assert ext_from_url("https://cdn/x/no-extension") == ".jpg"  # default


def test_plan_deletions_only_deletes_verified_backups(tmp_path):
    photos = tmp_path / "photos"
    photos.mkdir()
    (photos / "1.jpg").write_bytes(b"real bytes")  # verified backup
    (photos / "2.jpg").write_bytes(b"")            # present but EMPTY -> missing

    manifest = [
        {"id": "1", "file": "photos/1.jpg"},  # good
        {"id": "2", "file": "photos/2.jpg"},  # empty file
        {"id": "3", "file": None},            # never downloaded
    ]
    # live photo "4" was uploaded after the backup -> unbacked, must be skipped
    live_ids = ["1", "2", "3", "4"]

    deletable, unbacked, missing = plan_deletions(manifest, live_ids, str(tmp_path))

    assert deletable == ["1"], "only the verified backup may be deleted"
    assert unbacked == ["4"], "photos not in the manifest are never deleted"
    assert set(missing) == {"2", "3"}, "empty/absent backups are flagged incomplete"


def test_plan_deletions_empty_manifest_deletes_nothing(tmp_path):
    deletable, unbacked, missing = plan_deletions([], ["1", "2"], str(tmp_path))
    assert deletable == []
    assert unbacked == ["1", "2"]
    assert missing == []
