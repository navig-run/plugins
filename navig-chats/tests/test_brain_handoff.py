"""Sharing levels, the deterministic extractor, and the Continue-this-work pack."""

from __future__ import annotations

import json
import sqlite3

import pytest
from conftest import make_conversation, write_export_zip

from navig_chats import extract as ex
from navig_chats import handoff as ho
from navig_chats.chatgpt_export import import_export
from navig_chats.models import ConversationIn, MessageIn
from navig_chats.store import ChatStore, brain_shared


def live(native, msgs, title="Live"):
    return ConversationIn(provider="chatgpt", native_id=native, title=title, url=f"https://chatgpt.com/c/{native}",
                          messages=[MessageIn(id=i, role=r, text=t, created_at=ts) for i, r, t, ts in msgs],
                          completeness="rendered", provenance="live")


# ── sharing levels ──────────────────────────────────────────────────────────


def test_nothing_is_shared_by_default(store, sample_export):
    import_export(store, sample_export)
    assert brain_shared(store) == []


def test_folder_level_inherits_and_the_most_restrictive_wins(store, sample_export):
    import_export(store, sample_export)
    work = store.create_folder("Work")
    infra = store.create_folder("Infra", parent_id=work["id"])
    private = store.create_folder("Private")
    store.update_folder(work["id"], brain="agent", brain_project="navig")
    store.update_folder(private["id"], brain="off")
    store.bulk(["chatgpt:c-db"], "move", infra["id"])  # inherits agent + project from Work
    store.bulk(["chatgpt:c-rust"], "move", work["id"])
    store.bulk(["chatgpt:c-rust"], "add_to", private["id"])  # also private -> off
    shared = {r["id"]: (r["brain_level"], r["brain_project"]) for r in brain_shared(store)}
    assert shared == {"chatgpt:c-db": ("agent", "navig")}
    store.update("chatgpt:c-rust", brain="local")  # the chat's own choice wins
    shared = {r["id"]: r["brain_level"] for r in brain_shared(store)}
    assert shared == {"chatgpt:c-db": "agent", "chatgpt:c-rust": "local"}
    assert [r["id"] for r in brain_shared(store, min_level="agent")] == ["chatgpt:c-db"]
    store.update("chatgpt:c-db", brain="off")
    assert "chatgpt:c-db" not in {r["id"] for r in brain_shared(store)}


def test_bad_level_is_refused(store, sample_export):
    import_export(store, sample_export)
    with pytest.raises(ValueError):
        store.update("chatgpt:c-db", brain="everyone")
    f = store.create_folder("X")
    with pytest.raises(ValueError):
        store.update_folder(f["id"], brain="public")


def test_a_v1_library_upgrades_in_place(tmp_path):
    path = tmp_path / "old.db"
    s = ChatStore(path)
    s.upsert(live("keep", [("1", "user", "hello", None)]))
    conn = s._get_conn()
    conn.execute("ALTER TABLE conversations DROP COLUMN brain")
    conn.execute("ALTER TABLE folders DROP COLUMN brain")
    conn.execute("ALTER TABLE folders DROP COLUMN brain_project")
    conn.execute("UPDATE schema_version SET version = 1")
    conn.commit()
    s.close()
    s2 = ChatStore(path)
    try:
        assert s2.get_schema_version() == 2
        assert s2.get("chatgpt:keep")["brain"] is None
        assert s2.update("chatgpt:keep", brain="local")["brain"] == "local"
        assert s2.messages("chatgpt:keep")[0]["text"] == "hello"
    finally:
        s2.close()


# ── extraction ──────────────────────────────────────────────────────────────


def _conv(store, native="d1"):
    store.upsert(live(native, [
        ("u1", "user", "Decision (database): Postgres for everything.\nTODO: write the migration", "2026-01-01T10:00:00"),
        ("a1", "assistant", "Sounds good.\n- [ ] add an index\n- [x] create the schema\nDecision: also use Redis\n"
                            "```sql\nCREATE TABLE t (id int);\n```\nSee https://www.postgresql.org/docs/.", "2026-01-01T10:01:00"),
        ("u2", "user", "Décision (database) : SQLite with FTS5 instead — Postgres is overkill locally.", "2026-02-01T09:00:00"),
        ("a2", "assistant", "Agreed.", "2026-02-01T09:01:00"),
        ("u3", "user", "Open question: do we need encryption at rest?\nWhat about backups?", "2026-02-01T09:05:00"),
    ]))
    return store.get(f"chatgpt:{native}")


def test_extract_reads_explicit_markers_only_and_cites_messages(store):
    conv = _conv(store)
    items = ex.resolve(ex.extract(conv, store.messages(conv["id"]), conv["bookmarks"]))
    dec = {(i.text, i.status, i.basis, i.message_id) for i in items if i.kind == "decision"}
    assert ("Postgres for everything.", "superseded", "user", "u1") in dec
    assert ("SQLite with FTS5 instead — Postgres is overkill locally.", "current", "user", "u2") in dec
    assert ("also use Redis", "proposed", "assistant", "a1") in dec  # the assistant never decides
    tasks = {(i.text, i.status, i.basis) for i in items if i.kind == "task"}
    assert tasks == {("write the migration", "open", "user"), ("add an index", "open", "assistant"),
                     ("create the schema", "done", "assistant")}
    questions = [i for i in items if i.kind == "question"]
    assert {q.message_id for q in questions} == {"u3"} and len(questions) == 2  # marker + unanswered last question
    snip = next(i for i in items if i.kind == "snippet")
    assert snip.lang == "sql" and snip.message_id == "a1"
    assert any(i.kind == "link" and i.text == "https://www.postgresql.org/docs/" for i in items)
    sup = next(i for i in items if i.status == "superseded")
    assert sup.superseded_by == f"{conv['id']}#u2"


def test_marked_decisions_outrank_wording(store):
    conv = _conv(store, "d2")
    store.add_bookmark(conv["id"], message_id="a2", kind="decision", note="subject: database\nKeep SQLite, ship it")
    store.add_bookmark(conv["id"], message_id="a1", kind="todo", note="benchmark the index")
    conv = store.get(conv["id"])
    items = ex.resolve(ex.extract(conv, store.messages(conv["id"]), conv["bookmarks"]))
    current = [i for i in items if i.kind == "decision" and i.status == "current"]
    assert len(current) == 1 and current[0].basis == "marked" and current[0].message_id == "a2"
    assert any(i.kind == "task" and i.basis == "marked" and i.text == "benchmark the index" for i in items)


def test_restating_a_decision_is_not_superseding(store):
    store.upsert(live("r", [("1", "user", "Decision: ship on Friday", "2026-01-01"), ("2", "user", "Decision: ship on Friday", "2026-01-02")]))
    conv = store.get("chatgpt:r")
    items = [i for i in ex.resolve(ex.extract(conv, store.messages(conv["id"]))) if i.kind == "decision"]
    assert sorted(i.status for i in items) == ["current", "restated"]


# ── handoff pack ────────────────────────────────────────────────────────────


def test_handoff_pack_sections_citations_and_missing(store, sample_export):
    import_export(store, sample_export)
    conv = _conv(store)
    pack = ho.build_handoff(store, [conv["id"], "chatgpt:c-rust", "chatgpt:gone"], goal="Ship the cache layer")
    md = pack["markdown"]
    assert md.startswith(ho.PACK_MARKER)
    for heading in ("## Goal", "## Sources", "## Confirmed decisions", "## Open questions and tasks",
                    "## Proposed, not confirmed", "## Superseded decisions — do not revive", "## Relevant code",
                    "## Where it left off", "## References", "## Missing or incomplete context"):
        assert heading in md, heading
    assert f"{conv['url']}#navig-msg=u2" in md  # the current decision cites its message
    assert "~~Postgres for everything.~~" in md
    assert "```sql" in md and "CREATE TABLE t" in md
    missing = " ".join(pack["missing"])
    assert "`chatgpt:gone` is not in the library" in missing
    assert "saved from the page" in missing  # the live chat is not a complete export
    assert "other branches" in missing  # c-rust has a regenerated answer
    assert pack["counts"]["decision:current"] >= 1


def test_handoff_compact_and_errors(store, sample_export):
    import_export(store, sample_export)
    conv = _conv(store)
    pack = ho.build_handoff(store, [conv["id"]], compact=True)
    assert "## Relevant code" not in pack["markdown"] and "## Confirmed decisions" in pack["markdown"]
    with pytest.raises(LookupError):
        ho.build_handoff(store, ["chatgpt:nope"])


def test_handoff_into_a_space_never_overwrites_a_hand_written_file(store, tmp_path):
    conv = _conv(store)
    pack = ho.build_handoff(store, [conv["id"]])
    target = ho.write_into_space(tmp_path, pack)
    assert target.parent == tmp_path / ".navig" / "plans" / "briefs" and target.read_text(encoding="utf-8").startswith(ho.PACK_MARKER)
    assert ho.write_into_space(tmp_path, pack) == target  # our own file: refreshed
    target.write_text("# my notes\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        ho.write_into_space(tmp_path, pack)
    assert target.read_text(encoding="utf-8") == "# my notes\n"


# ── surfaces ────────────────────────────────────────────────────────────────


def test_cli_share_shared_handoff(db_path, sample_export, tmp_path):
    from typer.testing import CliRunner

    from navig_chats.commands.chats import app

    r = CliRunner()
    run = lambda *a: r.invoke(app, list(a), env={"COLUMNS": "120"})  # noqa: E731
    assert run("import", str(sample_export)).exit_code == 0
    assert json.loads(run("shared", "--json").output) == []
    assert run("share", "c-db", "--level", "local").exit_code == 0
    assert run("share", "c-db", "--level", "public").exit_code == 2
    assert [s["level"] for s in json.loads(run("shared", "--json").output)] == ["local"]
    res = run("share", "c-trip", "--level", "agent")
    assert res.exit_code == 0 and "AI provider" in res.output
    out = tmp_path / "next.md"
    assert run("handoff", "c-db", "c-trip", "-o", str(out), "--goal", "pick a database").exit_code == 0
    assert "## Goal" in out.read_text(encoding="utf-8")
    space = tmp_path / "space"
    assert run("handoff", "c-db", "--into", str(space)).exit_code == 0
    assert list((space / ".navig" / "plans" / "briefs").glob("chat-handoff-*.md"))
    assert run("handoff").exit_code == 2


def test_routes_brain_extract_handoff(db_path, sample_export):
    pytest.importorskip("aiohttp")
    from test_deck_routes import session

    s = ChatStore(db_path)
    import_export(s, sample_export)
    s.close()

    async def steps(c):
        r = await c.get("/api/deck/chats/brain")
        assert (await r.json())["data"]["shared"] == []
        r = await c.patch("/api/deck/chats/chatgpt:c-db", json={"brain": "local"})
        assert (await r.json())["data"]["brain"] == "local"
        r = await c.patch("/api/deck/chats/chatgpt:c-db", json={"brain": "public"})
        assert r.status == 400
        r = await c.get("/api/deck/chats/brain")
        assert [x["level"] for x in (await r.json())["data"]["shared"]] == ["local"]
        r = await c.get("/api/deck/chats/chatgpt:c-db/extract")
        assert "items" in (await r.json())["data"]
        r = await c.post("/api/deck/chats/handoff", json={"ids": ["chatgpt:c-db"], "goal": "decide"})
        data = (await r.json())["data"]
        assert data["filename"].startswith("chat-handoff-") and "## Goal" in data["markdown"]
        r = await c.get(data["url"])
        assert r.status == 200 and "attachment" in r.headers["Content-Disposition"]
        assert (await r.text()).startswith(ho.PACK_MARKER)
        r = await c.post("/api/deck/chats/handoff", json={"ids": ["chatgpt:nope"]})
        assert r.status == 404

    session(steps)


def test_split_export_still_imports(store, tmp_path):
    convs = [make_conversation(f"s{i}", f"Chat {i}", [("user", "x")]) for i in range(3)]
    assert import_export(store, write_export_zip(tmp_path / "s.zip", convs, split=2))["added"] == 3
    assert isinstance(sqlite3.sqlite_version, str)
