"""Exports stay readable without NAVIG, and a NAVIG archive restores what it holds."""

from __future__ import annotations

import json
import zipfile

from navig_chats import export as ex
from navig_chats.chatgpt_export import import_export
from navig_chats.store import ChatStore


def test_markdown_has_front_matter_and_keeps_code_fences(store, sample_export):
    import_export(store, sample_export)
    name, md = ex.export_one(store, "chatgpt:c-rust", "md")
    assert name.endswith(".md") and "Rust lifetimes" in name
    head, _, body = md.partition("\n---\n")
    assert head.startswith("---\nid: \"chatgpt:c-rust\"") and 'completeness: "complete"' in head
    assert "```rust\nfn longest" in body and "## You" in body and "## ChatGPT" in body
    assert "regenerated answer" not in body  # Markdown is the shown branch


def test_html_escapes_everything_and_is_self_contained(store):
    from navig_chats.models import ConversationIn, MessageIn

    store.upsert(ConversationIn(provider="chatgpt", native_id="h", title="<script>alert(1)</script>",
                                url="https://chatgpt.com/c/h", completeness="rendered", provenance="live",
                                messages=[MessageIn(id="1", role="user", text="<img src=x onerror=alert(1)>\n```js\n<b>\n```")]))
    _, page = ex.export_one(store, "chatgpt:h", "html")
    assert "<script>" not in page and "<img" not in page and "&lt;b&gt;" in page
    assert "<pre" in page and "http" not in page.split("<style>")[1].split("</style>")[0]
    assert "Library copy: rendered" in page


def test_zip_archive_manifest_checksums_and_folders(store, sample_export, tmp_path):
    import_export(store, sample_export)
    f = store.create_folder("Code")
    store.bulk(["chatgpt:c-rust"], "move", f["id"])
    dest = tmp_path / "out" / "archive.zip"
    manifest = ex.write_zip(store, ["chatgpt:c-rust", "chatgpt:c-db", "chatgpt:gone"], dest)
    assert dest.exists() and not list(dest.parent.glob("*.part"))
    assert manifest["count"] == 2 and manifest["missing"] == ["chatgpt:gone"]
    paths = {x["path"] for x in manifest["files"]}
    assert any(p.startswith("Code/") and p.endswith(".md") for p in paths)
    assert any(p.startswith("Unfiled/") and p.endswith(".json") for p in paths)
    assert ex.verify_zip(dest) == []
    with zipfile.ZipFile(dest) as zf:
        assert "README.txt" in zf.namelist()


def test_json_bundle_round_trips_through_restore(store, sample_export, tmp_path):
    import_export(store, sample_export)
    store.update("chatgpt:c-rust", alias="Lifetimes", tags=["rust", "learning"])
    dest = tmp_path / "a.zip"
    ex.write_zip(store, ["chatgpt:c-rust", "chatgpt:c-db"], dest, formats=("json",))
    before = store.messages("chatgpt:c-rust", branch="all")

    fresh = ChatStore(tmp_path / "fresh.db")
    try:
        res = ex.restore_bundle(fresh, dest)
        assert res["added"] == 2
        conv = fresh.get("chatgpt:c-rust")
        assert conv["alias"] == "Lifetimes" and conv["tags"] == ["learning", "rust"]
        assert conv["completeness"] == "complete" and conv["provenance"] == "export"
        after = fresh.messages("chatgpt:c-rust", branch="all")
        key = lambda m: (m["id"], m["role"], m["text"], m["on_active_branch"])  # noqa: E731
        assert sorted(map(key, after)) == sorted(map(key, before))
        assert ex.restore_bundle(fresh, dest)["unchanged"] == 2  # idempotent
    finally:
        fresh.close()


def test_restore_never_overwrites_current_organisation(store, sample_export, tmp_path):
    import_export(store, sample_export)
    store.update("chatgpt:c-db", alias="Old name")
    _, raw = ex.export_one(store, "chatgpt:c-db", "json")
    p = tmp_path / "one.json"
    p.write_text(raw, encoding="utf-8")
    store.update("chatgpt:c-db", alias="Current name")
    ex.restore_bundle(store, p)
    assert store.get("chatgpt:c-db")["alias"] == "Current name"
    assert json.loads(raw)["format"] == "navig-chats"


def test_filename_is_safe(store):
    conv = {"id": "chatgpt:x", "native_id": "abc-123", "display_title": 'a/b\\c:*?"<>|', "created_at": "2025-01-02T00:00:00"}
    name = ex.filename(conv, "md")
    assert name.startswith("2025-01-02-") and not any(c in name for c in '/\\:*?"<>|')
