"""Tests for navig-telegram-exports (navig telegram-exports)."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from navig_explore.telegram_exports import (
    _category,
    _norm,
    _resolve_final,
    _safe_name,
    telegram_app as app,
    classify_export,
)

runner = CliRunner()


# ── unit: category mapping ────────────────────────────────────────────────
@pytest.mark.parametrize("chat_type,expected", [
    ("personal_chat", "People"),
    ("bot_chat", "Bots"),
    ("private_channel", "Channels"),
    ("public_channel", "Channels"),
    ("private_supergroup", "Groups"),
    ("public_supergroup", "Groups"),
    ("group", "Groups"),
    ("", "Unsorted"),
    ("something_new", "Unsorted"),
])
def test_category(chat_type, expected):
    assert _category(chat_type) == expected


# ── unit: filename sanitisation ───────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("Normal Name", "Normal Name"),
    ("a/b", "a - b"),
    ("weird:name?", "weirdname"),
    ("  spaced  out  ", "spaced out"),
    ("trailing. ", "trailing"),
    ("♾️ AI Prompts [ VAULT ]", "♾️ AI Prompts [ VAULT ]"),  # emoji + brackets preserved
])
def test_safe_name(raw, expected):
    assert _safe_name(raw) == expected


# ── unit: loose-filename normalisation ────────────────────────────────────
@pytest.mark.parametrize("name,expected", [
    ("File.pdf", "file.pdf"),
    ("File (2).pdf", "file.pdf"),
    ("File__12345.pdf", "file.pdf"),
    ("File__dup.pdf", "file.pdf"),
    ("1 (3).jpg", "1.jpg"),
])
def test_norm(name, expected):
    assert _norm(name) == expected


# ── unit: classifier (json + html) ────────────────────────────────────────
def _mk(tmp_path, name, *, result=None, html=None):
    d = tmp_path / name
    d.mkdir()
    if result is not None:
        (d / "result.json").write_text(json.dumps(result), encoding="utf-8")
    if html is not None:
        (d / "messages.html").write_text(html, encoding="utf-8")
    return d


def test_classify_json(tmp_path):
    d = _mk(tmp_path, "ChatExport_2024-01-01",
            result={"name": "Bob", "type": "personal_chat", "id": 9})
    ident = classify_export(d)
    assert ident == {"name": "Bob", "type": "personal_chat",
                     "category": "People", "id": "9", "source": "json"}


def test_classify_html_group_created(tmp_path):
    # lowercase "created group «…»" is the real Telegram marker (regression)
    d = _mk(tmp_path, "ChatExport_2024-02-02", html=(
        '<div class="text bold">My Crew</div>'
        '<div class="message service"><div class="body details">'
        'Alice created group &laquo;My Crew&raquo; with members Alice, Bob</div></div>'))
    ident = classify_export(d)
    assert ident["category"] == "Groups"
    assert ident["source"] == "html"


def test_classify_html_channel(tmp_path):
    d = _mk(tmp_path, "ChatExport_2024-03-03", html=(
        '<div class="text bold">Feed</div>'
        '<div class="message service"><div class="body details">'
        'Channel &laquo;Feed&raquo; created</div></div>'))
    assert classify_export(d)["category"] == "Channels"


def test_classify_html_personal_gift(tmp_path):
    # private-exclusive marker beats a stray sender count
    d = _mk(tmp_path, "ChatExport_2024-04-04", html=(
        '<div class="text bold">Liz</div>'
        '<div class="message service"><div class="body details">'
        'void sent you a gift</div></div>'))
    assert classify_export(d)["category"] == "People"


def test_classify_none_when_empty(tmp_path):
    d = tmp_path / "ChatExport_2024-05-05"
    d.mkdir()
    assert classify_export(d) is None


# ── unit: chained-move resolver ───────────────────────────────────────────
def test_resolve_final_follows_chain(tmp_path):
    final = tmp_path / "Groups" / "X" / "d"
    final.mkdir(parents=True)
    moves = [
        (str(tmp_path / "Unsorted" / "X" / "d"), str(tmp_path / "People" / "X" / "d")),
        (str(tmp_path / "People" / "X"), str(tmp_path / "Groups" / "X")),
    ]
    # original dest was moved again; resolver should land on the real final dir
    assert _resolve_final(tmp_path / "Unsorted" / "X" / "d", moves) == final


# ── e2e: organize → audit ─────────────────────────────────────────────────
def test_organize_and_audit(tmp_path):
    _mk(tmp_path, "ChatExport_2024-01-15",
        result={"name": "Test Person", "type": "personal_chat", "id": 1})
    _mk(tmp_path, "ChatExport_2024-02-20", html=(
        '<div class="text bold">Crew</div>'
        '<div class="message service"><div class="body details">'
        'X created group &laquo;Crew&raquo; with members</div></div>'))

    dry = runner.invoke(app, ["organize", "--root", str(tmp_path)])
    assert dry.exit_code == 0 and "WOULD FILE" in dry.stdout

    ap = runner.invoke(app, ["organize", "--root", str(tmp_path), "--apply"])
    assert ap.exit_code == 0
    assert (tmp_path / "People" / "Test Person" / "2024-01-15" / "result.json").exists()
    assert (tmp_path / "Groups" / "Crew" / "2024-02-20" / "messages.html").exists()
    assert not (tmp_path / "ChatExport_2024-01-15").exists()  # moved, not copied

    audit = runner.invoke(app, ["audit", "--root", str(tmp_path)])
    assert audit.exit_code == 0
    assert "Unresolved (possible data loss): 0" in audit.stdout


# ── e2e: dedupe deletes only verified byte-duplicates ─────────────────────
def test_dedupe_confirm_deletes_only_verified(tmp_path):
    # a chat export containing a media file...
    export_media = tmp_path / "People" / "Jane" / "2024-01-01" / "files"
    export_media.mkdir(parents=True)
    (export_media / "cv.pdf").write_bytes(b"RESUME-BYTES")
    (tmp_path / "People" / "Jane" / "2024-01-01" / "result.json").write_text(
        '{"name":"Jane","type":"personal_chat","id":1}', encoding="utf-8")

    # ...and a loose 'matched' copy: one identical (dup), one same-name-different-bytes (keep)
    matched = tmp_path / "_staging" / "matched" / "Jane"
    matched.mkdir(parents=True)
    (matched / "cv.pdf").write_bytes(b"RESUME-BYTES")        # exact dup -> should delete
    (matched / "notes.pdf").write_bytes(b"UNIQUE")           # no export copy -> keep

    dry = runner.invoke(app, ["dedupe", "--root", str(tmp_path)])
    assert dry.exit_code == 0
    assert "DUPLICATE: 1" in dry.stdout and "ONLY-COPY: 1" in dry.stdout
    assert (matched / "cv.pdf").exists()  # dry-run keeps everything

    go = runner.invoke(app, ["dedupe", "--root", str(tmp_path), "--confirm"])
    assert go.exit_code == 0
    assert not (matched / "cv.pdf").exists()                 # verified dup deleted
    assert (matched / "notes.pdf").exists()                  # only-copy kept
    assert (export_media / "cv.pdf").exists()                # surviving copy intact
    log = (tmp_path / "_deletions-log.tsv").read_text(encoding="utf-8")
    assert "cv.pdf" in log and "notes.pdf" not in log


# ── e2e: match pairs loose files by name+size; --apply groups + dedups collisions ──
def _export_with_file(root, chat, date, fname, data):
    d = root / "People" / chat / date
    (d / "files").mkdir(parents=True)
    (d / "files" / fname).write_bytes(data)
    (d / "result.json").write_text(
        f'{{"name":"{chat}","type":"personal_chat","id":1,"messages":['
        f'{{"file":"files/{fname}","file_name":"{fname}","file_size":{len(data)}}}]}}',
        encoding="utf-8")


def test_match_report_strong(tmp_path):
    data = b"hello world"  # 11 bytes
    _export_with_file(tmp_path, "Bob", "2024-01-01", "doc.pdf", data)
    loose = tmp_path / "_staging" / "by-type" / "documents"
    loose.mkdir(parents=True)
    (loose / "doc.pdf").write_bytes(data)  # same name + exact size -> strong
    res = runner.invoke(app, ["match", "--root", str(tmp_path)])
    assert res.exit_code == 0
    assert "strong (name+size): 1" in res.stdout
    assert (loose / "doc.pdf").exists()  # report-only leaves it in place


def test_match_apply_groups_and_suffixes_collision(tmp_path):
    data = b"hello world"
    _export_with_file(tmp_path, "Bob", "2024-01-01", "doc.pdf", data)
    bt = tmp_path / "_staging" / "by-type"
    (bt / "documents").mkdir(parents=True)
    (bt / "other").mkdir(parents=True)
    (bt / "documents" / "doc.pdf").write_bytes(data)  # two loose files, same name+size,
    (bt / "other" / "doc.pdf").write_bytes(data)       # both strong-match the one chat
    res = runner.invoke(app, ["match", "--root", str(tmp_path), "--apply"])
    assert res.exit_code == 0
    grouped = tmp_path / "_staging" / "matched" / "Bob"
    files = sorted(p.name for p in grouped.iterdir())
    assert len(files) == 2, files          # both moved, neither clobbered
    assert "doc.pdf" in files
    assert any(n.startswith("doc__") and n.endswith(".pdf") for n in files)  # collision suffixed
