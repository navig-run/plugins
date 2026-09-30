"""Stage 3 — the pure TikTok comment-JSON parser (browser drive is integration-only)."""

from __future__ import annotations

from navig_download.tiktok.browser_fetch import (
    _COMMENTS_TAB_LABELS,
    _build_meta,
    _detail_from_bodies,
    _map_stats,
    _merge_raw,
    _parse_comments,
)


def test_comments_tab_labels_lowercase_and_multilingual():
    # The box-JS compares the tab's text against these already-lowercased — a stray capital would
    # silently miss the localized 'Comments' tab and the fetch would return no comments.
    assert all(lbl == lbl.lower() for lbl in _COMMENTS_TAB_LABELS)
    assert len(set(_COMMENTS_TAB_LABELS)) == len(_COMMENTS_TAB_LABELS)  # no duplicates
    assert "comments" in _COMMENTS_TAB_LABELS  # English (the common case must never regress)
    assert "commentaires" in _COMMENTS_TAB_LABELS and "コメント" in _COMMENTS_TAB_LABELS  # i18n


def test_parses_web_shape_and_ranks_by_likes():
    bodies = [{
        "comments": [
            {"cid": "1", "text": "meh", "digg_count": 2, "user": {"unique_id": "a"}},
            {"cid": "2", "text": "great!", "digg_count": 99, "user": {"nickname": "Bob"}},
        ]
    }]
    out = _parse_comments(bodies, limit=10)
    assert [c["text"] for c in out] == ["great!", "meh"]  # sorted by likes desc
    assert out[0]["author"] == "Bob"
    assert out[0]["likes"] == 99


def test_dedupes_across_pages_by_cid():
    bodies = [
        {"comments": [{"cid": "1", "text": "hi", "digg_count": 5}]},
        {"comments": [{"cid": "1", "text": "hi", "digg_count": 5}]},  # same page re-captured
    ]
    assert len(_parse_comments(bodies, 10)) == 1


def test_falls_back_to_text_id_when_no_cid():
    bodies = [
        {"comments": [{"text": "same", "digg_count": 1}]},
        {"comments": [{"text": "same", "digg_count": 1}]},
    ]
    assert len(_parse_comments(bodies, 10)) == 1


def test_skips_empty_and_malformed():
    bodies = [
        {"comments": [{"text": "  ", "digg_count": 1}, "notadict", {"digg_count": 3}]},
        {"no_comments_key": True},
        "notadict",
    ]
    assert _parse_comments(bodies, 10) == []


def test_respects_limit():
    bodies = [{"comments": [
        {"cid": str(i), "text": f"c{i}", "digg_count": i} for i in range(20)
    ]}]
    assert len(_parse_comments(bodies, 5)) == 5


def test_like_count_alias_and_app_author():
    bodies = [{"comments": [{"cid": "1", "text": "x", "like_count": 7, "author": "legacy"}]}]
    out = _parse_comments(bodies, 10)
    assert out[0]["likes"] == 7
    assert out[0]["author"] == "legacy"


# ── _map_stats (video stats / statsV2 string values) ──────────────────────────

def test_map_stats_int_keys():
    s = _map_stats({"playCount": 1000, "diggCount": 50, "commentCount": 8,
                    "shareCount": 3, "collectCount": 12})
    assert s == {"view_count": 1000, "like_count": 50, "comment_count": 8,
                 "repost_count": 3, "save_count": 12}


def test_map_stats_string_values_and_missing():
    # statsV2 gives strings; missing keys → None
    s = _map_stats({"playCount": "2500", "diggCount": "17"})
    assert s["view_count"] == 2500
    assert s["like_count"] == 17
    assert s["comment_count"] is None


def test_map_stats_empty():
    assert _map_stats({}) == {"view_count": None, "like_count": None,
                              "comment_count": None, "repost_count": None, "save_count": None}


# ── _build_meta (photo + video item struct → summary) ─────────────────────────

def test_build_meta_photo_post():
    raw = {
        "id": "999", "desc": "sunset carousel", "author": "photog", "author_name": "Photog",
        "stats": {"diggCount": 40, "commentCount": 5, "playCount": 900},
        "images": ["https://cdn/1.jpg", "https://cdn/2.jpg"], "is_photo": True,
        "create_time": 1700000000,
    }
    m = _build_meta("https://www.tiktok.com/@photog/photo/999", raw)
    assert m["is_photo"] is True
    assert m["description"] == "sunset carousel"
    assert m["uploader"] == "photog"
    assert m["images"] == ["https://cdn/1.jpg", "https://cdn/2.jpg"]
    assert m["like_count"] == 40 and m["view_count"] == 900
    assert m["id"] == "999"


def test_build_meta_none_is_safe():
    m = _build_meta("https://t/x", None)
    assert m["description"] == "" and m["images"] == [] and m["is_photo"] is False
    assert m["like_count"] is None


# ── _detail_from_bodies (enrich stats/images from intercepted item-detail JSON) ──

def test_detail_web_shape():
    bodies = [{"itemInfo": {"itemStruct": {
        "id": "5", "desc": "hi", "author": {"uniqueId": "u", "nickname": "N"},
        "stats": {"diggCount": 10, "playCount": 300},
        "imagePost": {"images": [{"imageURL": {"urlList": ["https://c/a.jpg"]}}]},
    }}}]
    d = _detail_from_bodies(bodies)
    assert d["id"] == "5" and d["author"] == "u" and d["is_photo"] is True
    assert d["images"] == ["https://c/a.jpg"]
    assert d["stats"]["diggCount"] == 10


def test_detail_app_shape():
    bodies = [{"aweme_detail": {
        "aweme_id": "9", "desc": "yo", "author": {"unique_id": "z", "nickname": "Z"},
        "statistics": {"digg_count": 4, "comment_count": 2, "play_count": 88},
        "image_post_info": {"images": [{"display_image": {"url_list": ["https://c/x.jpg"]}}]},
    }}]
    d = _detail_from_bodies(bodies)
    assert d["id"] == "9" and d["author"] == "z" and d["is_photo"] is True
    m = _build_meta("u", d)
    assert m["like_count"] == 4 and m["comment_count"] == 2 and m["view_count"] == 88
    assert m["images"] == ["https://c/x.jpg"]


def test_detail_none_when_absent():
    assert _detail_from_bodies([{"comments": [1]}, "x", {}]) is None


def test_merge_raw_detail_overrides_nonempty():
    base = {"id": "1", "desc": "og desc", "author": "url_handle", "images": [], "stats": {}}
    detail = {"id": "", "desc": "full desc", "images": ["a.jpg"], "stats": {"diggCount": 5}}
    m = _merge_raw(base, detail)
    assert m["desc"] == "full desc"        # non-empty detail wins
    assert m["author"] == "url_handle"     # empty detail id/author don't clobber base
    assert m["id"] == "1"
    assert m["images"] == ["a.jpg"]


def test_merge_raw_no_detail_returns_base():
    base = {"desc": "x"}
    assert _merge_raw(base, None) == base
