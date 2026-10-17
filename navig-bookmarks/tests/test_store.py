"""The store: FTS stays in sync, tag provenance holds, writes are transactional."""

from __future__ import annotations

from navig_bookmarks.models import ParsedBookmark


def test_add_get_and_fts_search(store) -> None:
    bid = store.add("https://docs.python.org/3/", title="Python docs", notes="stdlib reference",
                    folder="Dev/Python", tags=["Python", "Reference Docs"])
    bm = store.get(bid)
    assert bm.title == "Python docs" and bm.folder_path == "Dev/Python"
    assert bm.tags == {"python": "user", "reference-docs": "user"}
    assert [b.id for b in store.search("stdlib")] == [bid]
    assert [b.id for b in store.search("reference")] == [bid]  # tags are searchable
    assert [b.id for b in store.search("Dev")] == [bid]  # so is the folder
    assert store.search('c++ "unbalanced') == []  # bad FTS syntax never raises


def test_editing_twice_keeps_the_index_healthy(store) -> None:
    """The external-content trigger bug core's links store had: the 2nd edit corrupted FTS."""
    bid = store.add("https://example.com", title="old title")
    store.update(bid, title="middle title")
    store.update(bid, title="new title")
    assert store.search("old") == [] and store.search("middle") == []
    assert [b.id for b in store.search("new")] == [bid]
    assert store.delete(bid)
    assert store.search("new") == []
    store._get_conn().execute("INSERT INTO bookmarks_fts(bookmarks_fts) VALUES('integrity-check')")


def test_ai_rerun_replaces_only_ai_tags(store) -> None:
    bid = store.add("https://example.com", tags=["mine"])
    store.add_tags(bid, ["from-rule"], source="rule")
    added, removed = store.replace_source_tags(bid, ["alpha", "mine"], "ai")
    assert added == ["alpha"] and removed == []
    assert store.get(bid).tags == {"mine": "user", "from-rule": "rule", "alpha": "ai"}
    added, removed = store.replace_source_tags(bid, ["beta"], "ai")
    assert added == ["beta"] and removed == ["alpha"]
    assert store.get(bid).tags == {"mine": "user", "from-rule": "rule", "beta": "ai"}


def test_a_person_claiming_an_ai_tag_makes_it_theirs(store) -> None:
    bid = store.add("https://example.com")
    store.replace_source_tags(bid, ["keeper"], "ai")
    store.add_tags(bid, ["keeper"], source="user")
    store.replace_source_tags(bid, [], "ai")
    assert store.get(bid).tags == {"keeper": "user"}


def test_import_many_skips_duplicates_and_applies_rules(store) -> None:
    items = [
        ParsedBookmark(url="https://github.com/a/b", title="repo", folder="Dev"),
        ParsedBookmark(url="http://www.github.com/a/b/?utm_source=x", title="same repo"),
        ParsedBookmark(url="https://example.org", title="other", tags=["x"], added=1700000000),
    ]
    added, skipped = store.import_many(items, rule_tagger=lambda url: ["dev"] if "github" in url else [])
    assert (added, skipped) == (2, 1)
    again = store.import_many(items)
    assert again == (0, 3)
    repo = store.find_duplicate("https://github.com/a/b")
    assert repo.tags == {"dev": "rule"} and repo.folder_path == "Dev"
    other = store.find_duplicate("https://example.org")
    assert other.created_at.startswith("2023-11-14")


def test_merge_folds_tags_notes_and_visits(store) -> None:
    a = store.add("https://example.com/x", notes="first", tags=["one"])
    b = store.add("https://example.com/x?utm_source=y", notes="second", tags=["two"], vault_cred_id="cred-1")
    store.record_visit(b)
    assert store.merge(a, [b]) == 1
    kept = store.get(a)
    assert store.get(b) is None
    assert kept.tags == {"one": "user", "two": "user"}
    assert kept.notes == "first\nsecond" and kept.visit_count == 1 and kept.vault_cred_id == "cred-1"


def test_apply_folders_is_one_transaction(store) -> None:
    ids = [store.add(f"https://example.com/{i}") for i in range(3)]
    moved = store.apply_folders({ids[0]: "A", ids[1]: "B/C", ids[2]: ""})
    assert moved == 2  # the third already had no folder
    assert store.folders() == {"": 1, "A": 1, "B/C": 1}


def test_a_failed_transaction_rolls_back(store) -> None:
    import pytest

    with pytest.raises(RuntimeError):
        with store.transaction() as tx:
            store.add("https://rolled.back", conn=tx)
            raise RuntimeError("boom")
    assert store.count() == 0


def test_busy_timeout_is_set(store) -> None:
    timeout = store._get_conn().execute("PRAGMA busy_timeout").fetchone()[0]
    assert int(timeout) >= 5000


def test_resolve_id_accepts_a_unique_prefix(store) -> None:
    bid = store.add("https://example.com", bookmark_id="abcd1234")
    store.add("https://example.org", bookmark_id="abff0000")
    assert store.resolve_id("abcd").id == bid
    assert store.resolve_id("ab") is None  # too short / ambiguous
