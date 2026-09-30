"""Tests for ``navig telegram-exports notebook`` (Markdown library from a chat export).

Built on a synthetic Saved Messages export — never real data.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from navig_explore import telegram_notebook as nb
from navig_explore.telegram_exports import _category, telegram_app as app

runner = CliRunner()

VIDEO = b"\x00\x01video-bytes" * 50
PHOTO = b"\xff\xd8photo" * 20


def _msg(i, date, **kw):
    base = {"id": i, "type": "message", "date": date, "date_unixtime": "0",
            "from": "me", "from_id": "user1", "text": "", "text_entities": []}
    base.update(kw)
    if isinstance(base["text"], str) and base["text"] and not kw.get("text_entities"):
        base["text_entities"] = [{"type": "plain", "text": base["text"]}]
    return base


@pytest.fixture
def export(tmp_path: Path) -> Path:
    e = tmp_path / "Downloads" / "ChatExport_2026-09-26"
    for d, name, data in [
        ("photos", "photo_1@01-01-2024_10-00-00.jpg", PHOTO),
        ("photos", "photo_1@01-01-2024_10-00-00_thumb.jpg", b"thumb"),
        ("video_files", "clip.mp4", VIDEO),
        ("video_files", "clip (1).mp4", VIDEO),               # byte-identical copy
        ("video_files", "clip.mp4_thumb.jpg", b"vthumb"),
        ("video_files", "clip (1).mp4_thumb.jpg", b"vthumb1"),
        ("round_video_messages", "video.mp4", b"circle"),
        ("voice_messages", "audio_1@02-01-2024.ogg", b"voice"),
        ("files", "notes #1 (draft).pdf", b"%PDF"),
        ("files", "legalizer my_all_dump.sql.gz", b"dump"),
        ("stickers", "animatedsticker (1).tgs", b"tgs"),
        ("files", "song.mp3", b"ID3song"),
        ("files", "song.mp3_thumb.jpg", b"cover"),
        ("css", "style.css", b"body{}"),
    ]:
        (e / d).mkdir(parents=True, exist_ok=True)
        (e / d / name).write_bytes(data)
    msgs = [
        {"id": 1, "type": "service", "date": "2021-03-02T14:51:29", "date_unixtime": "0",
         "actor": "me", "action": "clear_history", "text": "", "text_entities": []},
        _msg(2, "2021-03-05T08:00:00", text=[{"type": "bold", "text": "Kill tabs:\n"},
                                            {"type": "pre", "text": "Stop-Process -Id 1", "language": "powershell"}],
             text_entities=[{"type": "bold", "text": "Kill tabs:\n"},
                            {"type": "pre", "text": "Stop-Process -Id 1", "language": "powershell"}]),
        _msg(3, "2022-06-01T12:00:00", text="https://vm.tiktok.com/ZMabc/?k=1",
             text_entities=[{"type": "link", "text": "https://vm.tiktok.com/ZMabc/?k=1"}]),
        _msg(4, "2022-06-01T12:00:30", text="that one is <b>great</b> *really*"),   # same note as 3
        _msg(5, "2023-07-27T09:00:00", text="FR7630006000011234567890189"),        # valid IBAN
        _msg(6, "2021-06-17T10:00:00", text="Username: me@example.com\nPassword: hunter22"),
        _msg(7, "2024-01-01T10:00:00", photo="photos/photo_1@01-01-2024_10-00-00.jpg",
             photo_file_size=len(PHOTO), width=10, height=10),
        _msg(8, "2024-02-01T10:00:00", file="video_files/clip.mp4", file_name="clip.mp4",
             thumbnail="video_files/clip.mp4_thumb.jpg", media_type="video_file",
             mime_type="video/mp4", duration_seconds=5, file_size=len(VIDEO)),
        _msg(9, "2024-03-01T10:00:00", file="video_files/clip (1).mp4", file_name="clip (1).mp4",
             thumbnail="video_files/clip (1).mp4_thumb.jpg", media_type="video_file",
             mime_type="video/mp4", duration_seconds=5, file_size=len(VIDEO)),
        _msg(10, "2024-05-27T23:35:53", file="round_video_messages/video.mp4",
             media_type="video_message", mime_type="video/mp4", duration_seconds=15, forwarded_from="Friend"),
        _msg(11, "2024-06-01T10:00:00", file="voice_messages/audio_1@02-01-2024.ogg",
             media_type="voice_message", mime_type="audio/ogg", duration_seconds=3),
        _msg(12, "2025-01-01T10:00:00", file="files/notes #1 (draft).pdf", file_name="notes #1 (draft).pdf",
             mime_type="application/pdf", file_size=4),
        _msg(13, "2025-02-01T10:00:00", file="files/legalizer my_all_dump.sql.gz",
             file_name="legalizer my_all_dump.sql.gz", mime_type="application/gzip", file_size=4),
        _msg(14, "2025-03-01T10:00:00", file="(File unavailable, please try again later)", file_size=0),
        _msg(15, "2025-04-01T10:00:00", text=None, rich_message={"blocks": [
            {"type": "heading", "level": 1, "text": {"type": "plain", "text": "Plan"}},
            {"type": "list", "kind": "ordered", "items": [
                {"task_state": "none", "content": "text", "num": "1",
                 "text": {"type": "bold", "text": {"type": "plain", "text": "first"}}}]},
            {"type": "divider"}]}, text_entities=None),
        _msg(16, "2025-05-01T10:00:00", text="https://youtu.be/abc123?si=xyz",
             text_entities=[{"type": "link", "text": "https://youtu.be/abc123?si=xyz"}]),
        _msg(17, "2025-05-09T10:00:00", text="https://www.youtube.com/watch?v=abc123",
             text_entities=[{"type": "link", "text": "https://www.youtube.com/watch?v=abc123"}]),
        _msg(18, "2025-06-01T10:00:00", reply_to_message_id=17, text="# not a heading\n- nor a list"),
        # a saved code snippet that itself contains src="…", plus user text that looks like links
        _msg(19, "2025-07-01T10:00:00", text=[{"type": "pre", "text": 'img.src = `<img src="${x}">`;', "language": "js"}],
             text_entities=[{"type": "pre", "text": 'img.src = `<img src="${x}">`;', "language": "js"},
                            {"type": "plain", "text": ' and <img src="nope.png"> and [x](missing.md)'}]),
        _msg(20, "2025-08-01T10:00:00", file="files/song.mp3", file_name="song.mp3",
             thumbnail="files/song.mp3_thumb.jpg", media_type="audio_file", mime_type="audio/mpeg",
             performer="Artist", title="Song", duration_seconds=61, file_size=7),
    ]
    (e / "result.json").write_text(json.dumps({"type": "saved_messages", "id": 1, "messages": msgs},
                                              ensure_ascii=False), encoding="utf-8")
    (e / "messages.html").write_text("<html></html>", encoding="utf-8")
    return e


@pytest.fixture
def lib_dir(tmp_path: Path, export: Path) -> Path:
    out = tmp_path / "Favorites"
    r = runner.invoke(app, ["notebook", "build", str(export), "--out", str(out), "--apply"])
    assert r.exit_code == 0, r.output
    return out


def _tree(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and "_state" not in p.parts}


# ── classifier ─────────────────────────────────────────────────────────────
def test_saved_messages_category():
    assert _category("saved_messages") == "Favorites"


# ── build + coverage ───────────────────────────────────────────────────────
def test_every_message_once_in_timeline_and_once_in_a_home(lib_dir: Path):
    def anchors(paths):
        ids = []
        for p in paths:
            ids += [int(x) for x in re.findall(r'<a id="m-(\d+)"></a>', p.read_text(encoding="utf-8"))]
        return ids
    tl = anchors((lib_dir / "Timeline").glob("*.md"))
    homes = anchors(list((lib_dir / "Topics").glob("*.md")) + list((lib_dir / "_sensitive").glob("*.md")))
    assert sorted(tl) == list(range(1, 21))
    assert sorted(homes) == list(range(1, 21))


def test_verify_passes_and_source_untouched(lib_dir: Path, export: Path):
    before = _tree(export)
    r = runner.invoke(app, ["notebook", "verify", str(lib_dir), "--deep"])
    assert r.exit_code == 0, r.output
    assert "PASS" in r.output
    assert _tree(export) == before
    # the copy is byte-identical to the source
    for rel, h in nb.read_manifest(lib_dir / "_state" / "manifest-2026-09-26.sha256").items():
        assert hashlib.sha256((export / rel).read_bytes()).hexdigest() == h


def test_verify_detects_a_missing_message(lib_dir: Path):
    tl = next((lib_dir / "Timeline").glob("2025.md"))
    tl.write_text(tl.read_text(encoding="utf-8").replace('<a id="m-16"></a>', ""), encoding="utf-8")
    r = runner.invoke(app, ["notebook", "verify", str(lib_dir)])
    assert r.exit_code == 1
    assert "FAIL" in r.output


def test_build_is_idempotent(lib_dir: Path, export: Path):
    first = _tree(lib_dir)
    r = runner.invoke(app, ["notebook", "build", str(export), "--out", str(lib_dir), "--apply"])
    assert r.exit_code == 0, r.output
    assert _tree(lib_dir) == first


# ── rendering ──────────────────────────────────────────────────────────────
def test_formatting_and_escaping(lib_dir: Path):
    y21 = (lib_dir / "Timeline" / "2021.md").read_text(encoding="utf-8")
    assert "**Kill tabs:**" in y21
    assert "```powershell\nStop-Process -Id 1\n```" in y21
    y22 = (lib_dir / "Timeline" / "2022.md").read_text(encoding="utf-8")
    assert "\\<b\\>great\\</b\\>" in y22 and "\\*really\\*" in y22     # user text never becomes HTML
    y25 = (lib_dir / "Timeline" / "2025.md").read_text(encoding="utf-8")
    assert "\\# not a heading" in y25 and "\\- nor a list" in y25
    assert "**Plan**" in y25 and "1. **first**" in y25 and "* * *" in y25   # rich_message
    assert "[↩ reply](2025.md#m-17)" in y25
    assert "media unavailable" in y25


def test_media_embeds_resolve(lib_dir: Path):
    y24 = (lib_dir / "Timeline" / "2024.md").read_text(encoding="utf-8")
    assert '<img src="../_export/2026-09-26/photos/photo_1%4001-01-2024_10-00-00.jpg"' in y24
    assert '<video src="../_export/2026-09-26/round_video_messages/video.mp4"' in y24
    assert "<audio src=" in y24
    # the duplicate video links to the kept copy, not its own clone
    assert "clip%20%281%29.mp4\"" not in y24.split('<a id="m-9"></a>')[1].split("<video src=")[1][:80]
    y25 = (lib_dir / "Timeline" / "2025.md").read_text(encoding="utf-8")
    assert "notes%20%231%20%28draft%29.pdf" in (lib_dir / "Media" / "Files.md").read_text(encoding="utf-8") \
        or "notes%20%231%20%28draft%29.pdf" in y25


# ── topics, links, similar ─────────────────────────────────────────────────
def test_topics(lib_dir: Path):
    _, msgs = nb.load_messages(lib_dir)
    nb.classify(msgs, {})
    t = {m.id: (m.topic, m.sensitive) for m in msgs}
    assert t[1] == ("system", "")
    assert t[2][0] == "dev"
    assert t[3][0] == "tiktok" and t[4][0] == "tiktok"        # the comment follows its link
    assert t[10][0] == "voice" and t[11][0] == "voice"
    assert t[5][1] == "finance" and t[6][1] == "credentials" and t[13][1] == "leaks"


def test_overrides_win(lib_dir: Path, export: Path):
    (lib_dir / "_state" / "topics.overrides.csv").write_text(
        "id,topic,reason\n4,quotes,it is a quote\n7,sensitive/finance,photo of a card\n", encoding="utf-8")
    runner.invoke(app, ["notebook", "build", "--out", str(lib_dir), "--apply"])
    _, msgs = nb.load_messages(lib_dir)
    nb.classify(msgs, nb.load_overrides(lib_dir))
    t = {m.id: m for m in msgs}
    assert t[4].topic == "quotes" and t[7].sensitive == "finance"
    assert '<a id="m-7"></a>' in (lib_dir / "_sensitive" / "finance.md").read_text(encoding="utf-8")


def test_sensitive_text_stays_out_of_timeline(lib_dir: Path):
    y21 = (lib_dir / "Timeline" / "2021.md").read_text(encoding="utf-8")
    assert "hunter22" not in y21 and "🔒" in y21
    assert "hunter22" in (lib_dir / "_sensitive" / "credentials.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("url,canon", [
    ("https://vm.tiktok.com/ZMabc/?k=1", "vm.tiktok.com/ZMabc"),
    ("https://youtu.be/abc123?si=xyz", "youtube.com/watch?v=abc123"),
    ("https://www.youtube.com/watch?v=abc123&feature=share", "youtube.com/watch?v=abc123"),
    ("https://youtube.com/shorts/abc123", "youtube.com/watch?v=abc123"),
    ("ryanair.com/fr/fr/fare-finder", "ryanair.com/fr/fr/fare-finder"),
    ("https://shop.com/p?id=7&utm_source=x&fbclid=y", "shop.com/p?id=7"),
])
def test_canonical_url(url, canon):
    assert nb.canonical_url(url) == canon


def test_links_deduped_and_similar(lib_dir: Path):
    yt = (lib_dir / "Links" / "youtube.md").read_text(encoding="utf-8")
    assert "youtube.com/watch?v=abc123" in yt and "| 2 |" in yt           # youtu.be + youtube.com = one link
    sim = (lib_dir / "Similar.md").read_text(encoding="utf-8")
    assert "clip.mp4" in sim and "clip%20%281%29.mp4" in sim


@pytest.mark.parametrize("text,kind", [
    ("FR7630006000011234567890189", "finance"),
    ("FR7630006000011234567890188", ""),                # bad checksum
    ("card 4111 1111 1111 1111", "finance"),
    ("order 1234567890123", ""),                       # not Luhn/card-shaped
    ("пароль: qwerty123", "credentials"),
    ("mot de passe oublié", ""),                       # talk about a password is not one
])
def test_sensitive_kind(text, kind):
    raw = {"text": text, "text_entities": [{"type": "plain", "text": text}]}
    assert nb.sensitive_kind(raw, text) == kind


# ── quarantine ─────────────────────────────────────────────────────────────
def test_quarantine_and_restore_roundtrip(lib_dir: Path):
    all_bytes = lambda: sorted(hashlib.sha256(p.read_bytes()).hexdigest()  # noqa: E731
                               for d in ("_export", "_duplicates") if (lib_dir / d).exists()
                               for p in (lib_dir / d).rglob("*") if p.is_file() and p.name != "manifest.tsv")
    before = all_bytes()
    r = runner.invoke(app, ["notebook", "quarantine", str(lib_dir), "--apply"])
    assert r.exit_code == 0, r.output
    assert (lib_dir / "_duplicates" / "2026-09-26" / "video_files" / "clip (1).mp4").exists()
    assert not (lib_dir / "_export" / "2026-09-26" / "video_files" / "clip (1).mp4").exists()
    assert runner.invoke(app, ["notebook", "verify", str(lib_dir)]).exit_code == 0
    assert all_bytes() == before
    r = runner.invoke(app, ["notebook", "restore", str(lib_dir)])
    assert r.exit_code == 0, r.output
    assert (lib_dir / "_export" / "2026-09-26" / "video_files" / "clip (1).mp4").exists()
    assert runner.invoke(app, ["notebook", "verify", str(lib_dir)]).exit_code == 0
    assert all_bytes() == before


def test_dry_run_writes_nothing(tmp_path: Path, export: Path):
    out = tmp_path / "Dry"
    r = runner.invoke(app, ["notebook", "build", str(export), "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert not out.exists() or not any(out.rglob("*.md"))


def test_cleanup_checklist(lib_dir: Path):
    c = (lib_dir / "CLEANUP-CHECKLIST.md").read_text(encoding="utf-8")
    assert "cannot be undone" in c
    assert "1 file(s) could not be exported" in c and "Timeline/2025.md#m-14" in c
    assert "CLEANUP-CHECKLIST.md" in (lib_dir / "README.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("text,topic", [
    ("j'ai reçu la lettre hier soir", "misc"),        # French "ai" is not AI
    ("new AI model released today", "ai"),
    ("stripe dashboard payouts", "misc"),              # "trip" inside "stripe"
    ("our trip to Nice in May", "travel"),
])
def test_keyword_rules_word_bounded(text, topic):
    m = nb.Msg(raw={"type": "message", "text": text,
                    "text_entities": [{"type": "plain", "text": text}]}, export="x", text=text)
    assert nb.rule_topic(m)[0] == topic


def test_verify_ignores_links_inside_user_code_and_text(lib_dir: Path):
    y25 = (lib_dir / "Timeline" / "2025.md").read_text(encoding="utf-8")
    assert '<img src="${x}">' in y25                    # kept verbatim inside the fence
    assert "nope.png" in y25 and "missing.md" in y25    # escaped user text, not a link
    assert runner.invoke(app, ["notebook", "verify", str(lib_dir)]).exit_code == 0


def test_audio_cover_is_linked(lib_dir: Path):
    audio = (lib_dir / "Media" / "Audio.md").read_text(encoding="utf-8")
    assert "song.mp3_thumb.jpg" in audio and "Artist – Song" in audio
