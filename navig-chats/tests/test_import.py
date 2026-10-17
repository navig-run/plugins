"""Importing ChatGPT's account export: tree walking, idempotence, split files, broken records."""

from __future__ import annotations

import io
import json

import pytest
from conftest import make_conversation, write_export_zip

from navig_chats.chatgpt_export import ExportFormatError, import_export, iter_json_array, parse_conversation


def test_iter_json_array_streams_across_chunk_boundaries(monkeypatch):
    from navig_chats import chatgpt_export

    monkeypatch.setattr(chatgpt_export, "CHUNK", 7)  # force objects to straddle reads
    items = [{"id": i, "text": "x" * (i * 5), "n": 12345.5} for i in range(20)]
    got = list(iter_json_array(io.StringIO("﻿  " + json.dumps(items))))
    assert got == items


def test_iter_json_array_rejects_a_non_array_and_broken_json():
    with pytest.raises(ExportFormatError):
        list(iter_json_array(io.StringIO('{"a": 1}')))
    with pytest.raises(ExportFormatError):
        list(iter_json_array(io.StringIO('[{"a": 1}, {"b": ')))


def test_parse_keeps_every_branch_and_marks_the_shown_one():
    raw = make_conversation("c1", "T", [("user", "q"), ("assistant", "shown answer")], branch_at=1, branch_text="old answer")
    conv = parse_conversation(raw)
    by_id = {m.id: m for m in conv.messages}
    assert set(by_id) == {"c1-m0", "c1-m1", "c1-alt1"}  # the hidden system root is gone
    assert by_id["c1-m1"].on_active_branch and not by_id["c1-alt1"].on_active_branch
    # skipped nodes leave no gap: the first visible turn has no visible parent
    assert by_id["c1-m0"].parent_id is None and by_id["c1-m1"].parent_id == "c1-m0"
    assert conv.completeness == "complete" and conv.provenance == "export"
    assert conv.url == "https://chatgpt.com/c/c1"
    assert conv.created_at.startswith("2023-11-14")


def test_parse_reads_project_code_and_attachments():
    raw = make_conversation("c2", "P", [("user", "hi")], project="g-p-xyz")
    node = raw["mapping"]["c2-m0"]["message"]
    node["content"] = {"content_type": "multimodal_text", "parts": [
        {"content_type": "image_asset_pointer", "asset_pointer": "file-service://file-1", "width": 10, "height": 10},
        "look at this"]}
    node["metadata"] = {"attachments": [{"name": "spec.pdf", "mime_type": "application/pdf", "size": 3}]}
    raw["mapping"]["c2-m0"]["children"] = ["code"]
    raw["mapping"]["code"] = {"id": "code", "parent": "c2-m0", "children": [], "message": {
        "id": "code", "author": {"role": "assistant"}, "create_time": 1, "metadata": {},
        "content": {"content_type": "code", "language": "python", "text": "print(1)"}}}
    raw["current_node"] = "code"
    conv = parse_conversation(raw)
    first, code = conv.messages
    assert conv.remote_project_id == "g-p-xyz" and conv.gizmo_id is None
    assert first.text == "look at this"
    assert {a["kind"] for a in first.attachments} == {"image_asset_pointer", "file"}
    assert all(a["available"] is False for a in first.attachments)
    assert code.has_code and code.text.startswith("```python")


def test_parse_without_current_node_takes_the_newest_leaf():
    raw = make_conversation("c3", "T", [("user", "q"), ("assistant", "a")], branch_at=1)
    raw["current_node"] = None
    conv = parse_conversation(raw)
    shown = [m.id for m in conv.messages if m.on_active_branch]
    assert shown == ["c3-m0", "c3-m1"]


def test_import_is_idempotent_and_keeps_the_persons_organisation(store, sample_export):
    first = import_export(store, sample_export)
    assert first["status"] == "done" and first["added"] == 3 and first["errors"] == []
    store.update("chatgpt:c-rust", alias="Lifetimes notes", tags=["rust"])
    again = import_export(store, sample_export)
    assert again["unchanged"] == 3 and again["added"] == again["updated"] == 0
    conv = store.get("chatgpt:c-rust")
    assert conv["alias"] == "Lifetimes notes" and conv["tags"] == ["rust"] and conv["revision"] == 1
    assert conv["message_count"] == 4 and conv["branch_count"] == 1


def test_import_split_files_and_a_broken_record(store, tmp_path):
    convs = [make_conversation(f"s{i}", f"Chat {i}", [("user", f"question {i}")]) for i in range(5)]
    convs.insert(2, {"title": "broken", "mapping": "not a tree"})
    path = write_export_zip(tmp_path / "split.zip", convs, split=2)
    res = import_export(store, path, batch=2)
    assert res["status"] == "partial"
    assert res["added"] == 5 and len(res["errors"]) == 1 and res["errors"][0]["title"] == "broken"
    job = store.get_import(res["job"])
    assert job["status"] == "partial" and job["added"] == 5


def test_import_a_changed_chat_bumps_its_revision(store, tmp_path):
    a = make_conversation("x", "Draft", [("user", "one")])
    import_export(store, write_export_zip(tmp_path / "a.zip", [a]))
    b = make_conversation("x", "Draft", [("user", "one"), ("assistant", "two")])
    res = import_export(store, write_export_zip(tmp_path / "b.zip", [b]))
    assert res["updated"] == 1
    assert store.get("chatgpt:x")["revision"] == 2


def test_import_plain_json_file(store, tmp_path):
    p = tmp_path / "conversations.json"
    p.write_text(json.dumps([make_conversation("j", "Json", [("user", "hello")])]), encoding="utf-8")
    assert import_export(store, p)["added"] == 1


def test_import_rejects_a_zip_without_conversations(store, tmp_path):
    import zipfile

    p = tmp_path / "other.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("photo.txt", "nope")
    with pytest.raises(ExportFormatError):
        import_export(store, p)
