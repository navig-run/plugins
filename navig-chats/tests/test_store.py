"""Store merges, organisation, folders and search ranking."""

from __future__ import annotations

import pytest

from navig_chats.chatgpt_export import import_export
from navig_chats.models import ConversationIn, MessageIn
from navig_chats.query import EmptyQuery, parse
from navig_chats.store import MARK_END, MARK_START, NotFound


def live(native, msgs, *, completeness="rendered", title="Live chat"):
    return ConversationIn(
        provider="chatgpt", native_id=native, title=title, url=f"https://chatgpt.com/c/{native}",
        messages=[MessageIn(id=i, role=r, text=t) for i, r, t in msgs], completeness=completeness, provenance="live",
    )


# ── query parsing ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expr", [
    ("rust", '"rust"'),
    ("rust lifetimes", '"rust" "lifetimes"'),
    ('"borrow checker"', '"borrow checker"'),
    ("life*", '"life"*'),
    ("rust -python", '("rust") NOT ("python")'),
    ("rust NOT python", '("rust") NOT ("python")'),
    ("rust OR go", '"rust" OR "go"'),
    ('a:b (c) ^d "e', '"a b" "c" "d" "e"'),
])
def test_query_parse(raw, expr):
    assert parse(raw).expression == expr


@pytest.mark.parametrize("raw", ["", "   ", "-python", "OR", '""', "-- ::"])
def test_query_with_nothing_to_find_is_rejected(raw):
    with pytest.raises(EmptyQuery):
        parse(raw)


# ── merging ───────────────────────────────────────────────────────────────────


def test_live_capture_never_downgrades_an_export(store, sample_export):
    import_export(store, sample_export)
    res = store.upsert(live("c-trip", [("c-trip-m0", "user", "Plan a weekend in Lisbon")], completeness="partial", title="ChatGPT"))
    conv = store.get("chatgpt:c-trip")
    assert res["status"] == "unchanged"
    assert conv["completeness"] == "complete" and conv["title"] == "Trip planning"  # generic page title ignored
    assert conv["last_visited_at"]


def test_live_capture_appends_new_turns_in_order(store, sample_export):
    import_export(store, sample_export)
    store.upsert(live("c-trip", [
        ("c-trip-m0", "user", "Plan a weekend in Lisbon"),
        ("c-trip-m1", "assistant", "Day one: Alfama and the tram."),
        ("new-1", "user", "And day two?"),
        ("new-2", "assistant", "Belém and pastéis."),
    ]))
    texts = [m["text"] for m in store.messages("chatgpt:c-trip")]
    assert texts[-2:] == ["And day two?", "Belém and pastéis."]
    conv = store.get("chatgpt:c-trip")
    assert conv["revision"] == 2 and conv["completeness"] == "complete" and conv["message_count"] == 4


def test_rendered_capture_switches_the_active_branch(store, sample_export):
    import_export(store, sample_export)
    store.upsert(live("c-rust", [
        ("c-rust-m0", "user", "How do lifetimes work in Rust borrow checking?"),
        ("c-rust-alt1", "assistant", "A regenerated answer about ownership"),
    ]))
    active = [m["id"] for m in store.messages("chatgpt:c-rust")]
    assert active == ["c-rust-m0", "c-rust-alt1"]
    assert len(store.messages("chatgpt:c-rust", branch="all")) == 5  # nothing deleted


def test_new_live_chat_and_url_only_bookmark(store):
    store.upsert(live("abc", [("1", "user", "hi"), ("2", "assistant", "hello")]))
    assert store.get("chatgpt:abc")["completeness"] == "rendered"
    store.upsert(ConversationIn(provider="chatgpt", native_id="zzz", title="Link", url="https://chatgpt.com/c/zzz",
                                messages=[], completeness="url_only", provenance="manual"))
    conv = store.get("chatgpt:zzz")
    assert conv["completeness"] == "url_only" and conv["message_count"] == 0


def test_record_visit_only_for_saved_chats(store):
    assert store.record_visit("chatgpt:nope") is None
    store.upsert(live("v", [("1", "user", "x")]))
    assert store.record_visit("chatgpt:v")["last_visited_at"]


# ── organisation ──────────────────────────────────────────────────────────────


def test_folders_tree_move_and_delete(store, sample_export):
    import_export(store, sample_export)
    work = store.create_folder("Work")
    sub = store.create_folder("Infra", parent_id=work["id"])
    store.bulk(["chatgpt:c-db"], "move", sub["id"])
    store.bulk(["chatgpt:c-rust"], "add_to", work["id"])
    rows, total = store.list(folder=work["id"])  # a folder includes its subfolders
    assert total == 2 and {r["id"] for r in rows} == {"chatgpt:c-db", "chatgpt:c-rust"}
    with pytest.raises(ValueError):
        store.update_folder(work["id"], parent_id=sub["id"])  # no cycles
    assert store.list(collection="uncategorized")[1] == 1
    store.delete_folder(work["id"])
    folders = store.folders()
    assert [f["name"] for f in folders] == ["Infra"] and folders[0]["parent_id"] is None
    assert store.get("chatgpt:c-rust") is not None  # chats survive their folder
    with pytest.raises(NotFound):
        store.bulk(["chatgpt:c-rust"], "move", "missing")


def test_sorts_collections_and_flags(store, sample_export):
    import_export(store, sample_export)
    newest = [r["id"] for r in store.list(sort="newest")[0]]
    oldest = [r["id"] for r in store.list(sort="oldest")[0]]
    assert newest == list(reversed(oldest))
    store.update("chatgpt:c-rust", pinned=True)
    assert store.list(sort="newest")[0][0]["id"] == "chatgpt:c-rust"  # pinned first
    store.bulk(["chatgpt:c-trip"], "archive")
    assert "chatgpt:c-trip" not in {r["id"] for r in store.list()[0]}
    assert store.list(collection="archived")[1] == 1
    assert store.list(collection="projects")[0][0]["remote_project_id"] == "g-p-abc123"
    counts = store.collection_counts()
    assert counts["all"] == 2 and counts["pinned"] == 1 and counts["archived"] == 1


def test_alias_is_searchable_and_separate_from_the_title(store, sample_export):
    import_export(store, sample_export)
    store.update("chatgpt:c-trip", alias="Portugal holiday")
    hit = store.search("portugal")["results"][0]
    assert hit["id"] == "chatgpt:c-trip" and hit["title"] == "Trip planning" and hit["display_title"] == "Portugal holiday"


def test_bookmarks(store, sample_export):
    import_export(store, sample_export)
    b = store.add_bookmark("chatgpt:c-db", message_id="c-db-m1", kind="decision", note="final call")
    assert store.get("chatgpt:c-db")["bookmarks"][0]["id"] == b["id"]
    with pytest.raises(ValueError):
        store.add_bookmark("chatgpt:c-db", message_id=None, kind="nonsense")
    assert store.remove_bookmark(b["id"])


# ── search ────────────────────────────────────────────────────────────────────


def test_search_ranks_relevance_and_points_at_the_message(store, sample_export):
    import_export(store, sample_export)
    res = store.search("lifetimes")
    top = res["results"][0]
    assert top["id"] == "chatgpt:c-rust" and top["title_match"]
    assert {h["message_id"] for h in top["hits"]} <= {"c-rust-m0", "c-rust-m1", "c-rust-m3"}
    assert MARK_START in top["hits"][0]["snippet"] and MARK_END in top["hits"][0]["snippet"]


def test_search_phrase_exclusion_prefix_role_code(store, sample_export):
    import_export(store, sample_export)
    assert [r["id"] for r in store.search('"local index"')["results"]] == ["chatgpt:c-db"]
    assert store.search('"index local"')["results"] == []
    assert "chatgpt:c-db" not in {r["id"] for r in store.search("sqlite -postgres")["results"]}
    assert [r["id"] for r in store.search("alfa*")["results"]] == ["chatgpt:c-trip"]
    assert store.search("lisbon", role="assistant")["results"] == []
    code = store.search("longest", code_only=True)["results"]
    assert [r["id"] for r in code] == ["chatgpt:c-rust"]
    # the hidden branch is searchable too (it is in the archive)
    assert [r["id"] for r in store.search("ownership")["results"]] == ["chatgpt:c-rust"]


def test_search_within_one_conversation_returns_ordered_hits(store, sample_export):
    import_export(store, sample_export)
    res = store.search("lifetimes", conversation="chatgpt:c-rust")
    [only] = res["results"]
    positions = [h["position"] for h in only["hits"]]
    assert positions == sorted(positions) and len(positions) >= 2


def test_search_filters_by_folder_and_survives_hostile_input(store, sample_export):
    import_export(store, sample_export)
    f = store.create_folder("Travel")
    store.bulk(["chatgpt:c-trip"], "move", f["id"])
    assert [r["id"] for r in store.search("day", folder=f["id"])["results"]] == ["chatgpt:c-trip"]
    for hostile in ['"', "NEAR(a b)", "col:x", "a AND", "*", "))((", "'; DROP TABLE messages; --"]:
        try:
            store.search(hostile)
        except EmptyQuery:
            pass


def test_index_integrity_and_rebuild(store, sample_export):
    import_export(store, sample_export)
    store.delete("chatgpt:c-trip")  # cascades through the FTS delete trigger
    assert store.integrity()
    assert store.search("lisbon")["results"] == []
    store.rebuild_index()
    assert store.integrity() and store.search("lifetimes")["results"]


def test_unicode_and_diacritics(store):
    store.upsert(live("u", [("1", "user", "Café crème à Paris"), ("2", "assistant", "東京 は 楽しい")]))
    assert store.search("cafe creme")["results"]
    assert store.search("東京")["results"]


def test_search_over_budget_falls_back_to_newest_matches_and_says_so(store, sample_export, monkeypatch):
    from navig_chats import store as store_mod

    import_export(store, sample_export)
    assert store.search("lifetimes")["ranking"] == "relevance"

    def over(*_a, **_k):
        raise store_mod._OverBudget

    monkeypatch.setattr(store, "_read_budgeted", over)
    res = store.search("lifetimes")
    assert res["ranking"] == "recent" and res["results"][0]["id"] == "chatgpt:c-rust"
    assert res["results"][0]["hits"][0]["snippet"]


def test_budget_interrupt_really_fires(store, sample_export):
    import_export(store, sample_export)
    store.SEARCH_BUDGET_S = -1  # already past the deadline at the first progress callback
    from navig_chats import store as store_mod

    conn = store._get_conn()
    conn.execute("CREATE TEMP TABLE big(x)")
    conn.execute("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i < 200000) INSERT INTO big SELECT i FROM n")
    with pytest.raises(store_mod._OverBudget):
        store._read_budgeted("SELECT SUM(x) FROM big ORDER BY 1", ())
    assert conn.execute("SELECT 1").fetchone()[0] == 1  # handler cleared, connection healthy


def test_live_capture_dates_when_navig_saw_new_content(store):
    store.upsert(live("d", [("1", "user", "x")]))
    first = store.get("chatgpt:d")["updated_at"]
    assert first
    store.upsert(live("d", [("1", "user", "x")]))
    assert store.get("chatgpt:d")["updated_at"] == first  # nothing new: no new date
    store.upsert(live("d", [("1", "user", "x"), ("2", "assistant", "y")]))
    assert store.get("chatgpt:d")["updated_at"] >= first
