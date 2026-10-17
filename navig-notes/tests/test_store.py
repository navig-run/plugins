"""The sync contract: the higher revision wins, deletes travel, nothing is lost or echoed back."""

from __future__ import annotations

import json

import pytest

from conftest import dock_note
from navig_notes.store import NoteError, NoteStore, keys_for_url, match_query, normalize


def test_a_pushed_note_round_trips_verbatim(store):
    n = dock_note(futureField={"kept": True})  # a field a newer Dock adds
    out = store.sync([n], [], since=0)
    assert out["results"]["added"] == 1
    assert out["changes"] == [], "the client's own push is not echoed back"
    got = store.get("n1")
    assert got["geom"] == n["geom"] and got["futureField"] == {"kept": True}
    assert got["origin"] == "dock" and got["share"] == "local"


def test_higher_revision_wins_older_never_overwrites(store):
    store.sync([dock_note(rev=3, body="v3")], [])
    out = store.sync([dock_note(rev=2, body="stale v2")], [], since=0)
    assert out["results"]["stale"] == 1
    assert store.get("n1")["body"] == "v3"
    # The client that pushed the stale copy is told the stored one.
    assert [c["body"] for c in out["changes"]] == ["v3"]
    out = store.sync([dock_note(rev=4, body="v4")], [])
    assert out["results"]["updated"] == 1 and store.get("n1")["body"] == "v4"


def test_equal_revision_later_write_wins(store):
    store.sync([dock_note(rev=2, body="a", updated=100)], [])
    assert store.sync([dock_note(rev=2, body="b", updated=200)], [])["results"]["updated"] == 1
    out = store.sync([dock_note(rev=2, body="c", updated=150)], [])
    assert out["results"]["stale"] == 1
    assert [c["body"] for c in out["changes"]] == ["b"], "the older writer is handed the stored copy"
    assert store.get("n1")["body"] == "b"
    assert store.sync([dock_note(rev=2, body="b", updated=200)], [])["results"]["unchanged"] == 1


def test_cursor_returns_only_what_changed_elsewhere(store):
    first = store.sync([dock_note("a"), dock_note("b")], [], since=0)
    cur = first["cursor"]
    made = store.create(body="from the terminal", origin="cli")
    out = store.sync([], [], since=cur)
    assert [c["id"] for c in out["changes"]] == [made["id"]]
    assert out["changes"][0]["origin"] == "cli"
    assert store.sync([], [], since=out["cursor"])["changes"] == []


def test_deletes_travel_both_ways(store):
    store.sync([dock_note("a", rev=2)], [])
    cur = store.cursor()
    # From the Dock: a tombstone at the same revision deletes.
    out = store.sync([], [{"id": "a", "rev": 2, "deletedAt": 123}], since=cur)
    assert out["results"]["deleted"] == 1 and out["changes"] == []
    assert store.get("a") is None and store.get("a", include_deleted=True)["deletedAt"] == 123
    # A note edited here after the Dock last saw it is NOT deleted by a stale tombstone.
    store.sync([dock_note("b", rev=5)], [])
    assert store.sync([], [{"id": "b", "rev": 4}])["results"]["deleted"] == 0
    assert store.get("b") is not None
    # From here: delete goes one revision ahead and reaches the Dock as a tombstone.
    cur = store.cursor()
    assert store.delete("b") == "deleted"
    ch = store.sync([], [], since=cur)["changes"]
    assert ch == [{"id": "b", "rev": 6, "deleted": True, "deletedAt": ch[0]["deletedAt"]}]
    # Restore = the Dock pushes the note at a higher revision.
    store.sync([dock_note("b", rev=7, body="restored")], [])
    assert store.get("b")["body"] == "restored"


def test_bad_notes_are_rejected_one_by_one(store):
    out = store.sync([dock_note("ok"), {"id": ""}, {"id": "x y"}, "nope", dock_note("p", scope="page", pageKey="")], [])
    assert out["results"]["added"] == 1
    assert len(out["results"]["rejected"]) == 4
    # A long body is cut to the Dock's own limit; a note far beyond any real size is refused.
    assert len(normalize({"id": "long", "scope": "global", "body": "x" * 300_000})["body"]) == 100_000
    with pytest.raises(NoteError):
        normalize({"id": "big", "scope": "global", "blob": "x" * 300_000})


def test_search_finds_words_tags_and_sites_not_deleted(store):
    store.sync([
        dock_note("a", body="Check the retry path before Friday", tags=["work"]),
        dock_note("b", body="Lisbon trip ideas", scope="site", siteKey="travel.example", pageKey=""),
        dock_note("c", body="retry later", scope="global", pageKey="", siteKey=""),
    ], [])
    assert {n["id"] for n in store.search("retry")} == {"a", "c"}
    assert [n["id"] for n in store.search("#work")] == ["a"]
    assert [n["id"] for n in store.search("travel")] == ["b"]
    assert "\x02" in store.search("retry")[0]["snippet"]
    store.sync([], [{"id": "c", "rev": 1}])
    assert {n["id"] for n in store.search("retry")} == {"a"}
    assert store.search('"before friday"')[0]["id"] == "a"
    assert store.search("") == []
    assert match_query('" OR *') == '"OR"*', "FTS operators are quoted into plain words"
    assert store.search("OR") == [] and store.search("NEAR(") == []


def test_sharing_level_follows_the_dock_setting(store):
    store.sync([dock_note("a")], [], share="local")
    assert [n["id"] for n in store.shared("agent")] == []
    store.sync([dock_note("a")], [], share="agent")  # same revision, only the level moved
    assert [n["id"] for n in store.shared("agent")] == ["a"]
    assert store.stats()["agent"] == 1


def test_create_keys_a_typed_url_like_the_dock(store):
    n = store.create(body="x", url="https://www.example.com/a/?utm_source=x&b=2&token=SECRET#/route", scope="page")
    assert n["pageKey"] == "example.com/a?b=2#/route" and n["siteKey"] == "example.com"
    with pytest.raises(NoteError):
        store.create(body="x", scope="page")
    assert keys_for_url("chrome://settings") is None
    assert keys_for_url("http://localhost:3000/x")["siteKey"] == "localhost:3000"


def test_store_survives_reopen_and_json_is_valid(db_path):
    s = NoteStore(db_path)
    s.sync([dock_note("a")], [])
    s.close()
    s2 = NoteStore(db_path)
    assert s2.get("a")["body"] == "hello"
    raw = s2._get_conn().execute("SELECT data FROM notes").fetchone()[0]
    assert json.loads(raw)["id"] == "a"
    assert s2.purge(older_than_days=0) == 0
    s2.close()
