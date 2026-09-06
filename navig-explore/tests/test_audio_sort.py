"""Planning and applying an audio-sort run — the part that touches real files.

Every test here guards a way a library reorganisation can quietly lose data:
overwriting a same-named file, orphaning a transcript sidecar, or filing a coin-flip
prediction as if it were certain.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from navig_explore import audio_sort as asort


def _rec(path, label, conf, dur=10.0):
    return {"path": str(path), "label": label, "confidence": conf, "dur": dur}


def test_plan_routes_confident_files_and_sends_the_rest_to_review(tmp_path):
    src, music, voice = tmp_path / "src", tmp_path / "music", tmp_path / "voice"
    src.mkdir()
    a, b = src / "a.mp3", src / "b.mp3"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    rows = asort.plan([_rec(a, "music", 0.95), _rec(b, "music", 0.40)],
                      {"music": music, "voice": voice},
                      min_conf=0.6, review_root=tmp_path / "_review")
    by = {r["name"]: r for r in rows}
    assert by["a.mp3"]["dest"] == str(music / "a.mp3")
    assert by["a.mp3"]["review"] is False
    assert by["b.mp3"]["review"] is True
    assert by["b.mp3"]["dest"] == str(tmp_path / "_review" / "music" / "b.mp3")
    assert "0.40" in by["b.mp3"]["reason"]


def test_plan_sends_an_unrouted_label_to_review_instead_of_dropping_it(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    f = src / "x.mp3"
    f.write_bytes(b"x")
    rows = asort.plan([_rec(f, "sfx", 0.99)], {"music": tmp_path / "m"},
                      min_conf=0.6, review_root=tmp_path / "_review")
    assert rows[0]["review"] is True
    assert rows[0]["dest"] == str(tmp_path / "_review" / "sfx" / "x.mp3")


def test_apply_moves_files_and_writes_a_reversible_log(tmp_path):
    src, dest = tmp_path / "src", tmp_path / "music"
    src.mkdir()
    f = src / "song.mp3"
    f.write_bytes(b"audio")
    rows = asort.plan([_rec(f, "music", 0.9)], {"music": dest}, min_conf=0.6)
    log = tmp_path / "log.jsonl"
    stats = asort.apply(rows, log_path=log)

    assert stats["moved"] == 1
    assert not f.exists()
    assert (dest / "song.mp3").read_bytes() == b"audio"
    entry = json.loads(log.read_text(encoding="utf-8").strip())
    assert entry["src"] == str(f) and entry["dst"] == str(dest / "song.mp3")


def test_apply_never_overwrites_an_existing_destination(tmp_path):
    src, dest = tmp_path / "src", tmp_path / "music"
    src.mkdir()
    dest.mkdir()
    (dest / "song.mp3").write_bytes(b"ORIGINAL")
    f = src / "song.mp3"
    f.write_bytes(b"NEW")
    rows = asort.plan([_rec(f, "music", 0.9)], {"music": dest}, min_conf=0.6)
    asort.apply(rows, log_path=tmp_path / "log.jsonl")

    assert (dest / "song.mp3").read_bytes() == b"ORIGINAL"
    assert (dest / "song (2).mp3").read_bytes() == b"NEW"


def test_apply_carries_the_transcript_sidecar_along(tmp_path):
    """A clip's .md transcript shares its stem; leaving it behind breaks the pairing."""
    src, dest = tmp_path / "src", tmp_path / "voice"
    src.mkdir()
    (src / "clip.m4a").write_bytes(b"audio")
    (src / "clip.md").write_text("transcript", encoding="utf-8")
    (src / "unrelated.m4a").write_bytes(b"other")
    rows = asort.plan([_rec(src / "clip.m4a", "voice", 0.9)], {"voice": dest}, min_conf=0.6)
    stats = asort.apply(rows, log_path=tmp_path / "log.jsonl")

    assert stats["companions"] == 1
    assert (dest / "clip.md").read_text(encoding="utf-8") == "transcript"
    assert (src / "unrelated.m4a").exists()      # untouched: different stem


def test_undo_restores_every_moved_file(tmp_path):
    src, dest = tmp_path / "src", tmp_path / "music"
    src.mkdir()
    for n in ("a.mp3", "b.mp3"):
        (src / n).write_bytes(n.encode())
    rows = asort.plan([_rec(src / "a.mp3", "music", 0.9), _rec(src / "b.mp3", "music", 0.9)],
                      {"music": dest}, min_conf=0.6)
    log = tmp_path / "log.jsonl"
    asort.apply(rows, log_path=log)
    assert not (src / "a.mp3").exists()

    stats = asort.undo(log)
    assert stats["restored"] == 2
    assert (src / "a.mp3").read_bytes() == b"a.mp3"
    assert (src / "b.mp3").read_bytes() == b"b.mp3"


def test_apply_counts_a_vanished_source_instead_of_crashing(tmp_path):
    rows = asort.plan([_rec(tmp_path / "ghost.mp3", "music", 0.9)],
                      {"music": tmp_path / "m"}, min_conf=0.6)
    assert asort.apply(rows, log_path=tmp_path / "log.jsonl") == {"missing": 1}


def test_summarize_splits_filed_from_review(tmp_path):
    src = tmp_path / "s"
    src.mkdir()
    for n in ("a.mp3", "b.mp3", "c.mp3"):
        (src / n).write_bytes(b"x")
    rows = asort.plan([_rec(src / "a.mp3", "music", 0.9), _rec(src / "b.mp3", "music", 0.2),
                       _rec(src / "c.mp3", "voice", 0.8)],
                      {"music": tmp_path / "m", "voice": tmp_path / "v"}, min_conf=0.6)
    s = asort.summarize(rows)
    assert s == {"total": 3, "filed": {"music": 1, "voice": 1},
                 "review": {"music": 1}, "review_total": 1}


def test_prune_empty_removes_emptied_dirs_but_keeps_files(tmp_path):
    (tmp_path / "empty" / "deep").mkdir(parents=True)
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "keep.mp3").write_bytes(b"x")
    assert asort.prune_empty(tmp_path) == 2
    assert not (tmp_path / "empty").exists()
    assert (tmp_path / "full" / "keep.mp3").exists()


# ── scoping: turn a whole-folder reorg into a targeted cleanup ───────────────
def test_min_dur_leaves_short_files_alone(tmp_path):
    """Pull the hour-long album out of the SFX library; don't touch the 3 s stings."""
    src, music = tmp_path / "sfx", tmp_path / "music"
    src.mkdir()
    short, long = src / "sting.wav", src / "album.mp3"
    short.write_bytes(b"x")
    long.write_bytes(b"x")
    rows = asort.plan([_rec(short, "music", 0.99, dur=3.0),
                       _rec(long, "music", 0.99, dur=3600.0)],
                      {"music": music}, min_conf=0.6, min_dur=120)
    by = {r["name"]: r for r in rows}
    assert by["sting.wav"]["action"] == "leave"
    assert by["sting.wav"]["dest"] == str(short)      # destination is where it already is
    assert by["album.mp3"]["action"] == "move"


def test_out_of_scope_files_are_reported_not_dropped(tmp_path):
    """Filtering them out silently would make the plan look like it covered the folder."""
    src = tmp_path / "s"
    src.mkdir()
    for n in ("a.mp3", "b.mp3"):
        (src / n).write_bytes(b"x")
    rows = asort.plan([_rec(src / "a.mp3", "music", 0.99, dur=5.0),
                       _rec(src / "b.mp3", "music", 0.99, dur=500.0)],
                      {"music": tmp_path / "m"}, min_conf=0.6, min_dur=120)
    assert len(rows) == 2
    assert asort.summarize(rows) == {"total": 2, "filed": {"music": 1}, "review": {},
                                     "review_total": 0, "left_in_place": 1}


def test_max_dur_leaves_long_files_alone(tmp_path):
    src = tmp_path / "s"
    src.mkdir()
    (src / "long.mp3").write_bytes(b"x")
    rows = asort.plan([_rec(src / "long.mp3", "music", 0.99, dur=900.0)],
                      {"music": tmp_path / "m"}, min_conf=0.6, max_dur=300)
    assert rows[0]["action"] == "leave"


def test_keep_leaves_a_whole_class_in_place(tmp_path):
    """A cleanup routes the misfits out and must not re-file the class that belongs."""
    src = tmp_path / "sfx"
    src.mkdir()
    for n in ("boom.wav", "song.mp3"):
        (src / n).write_bytes(b"x")
    rows = asort.plan([_rec(src / "boom.wav", "sfx", 0.99, dur=500.0),
                       _rec(src / "song.mp3", "music", 0.99, dur=500.0)],
                      {"music": tmp_path / "m"}, min_conf=0.6, keep={"sfx"})
    by = {r["name"]: r for r in rows}
    assert by["boom.wav"]["action"] == "leave"
    assert "kept" in by["boom.wav"]["reason"]
    assert by["song.mp3"]["action"] == "move"


def test_kept_class_is_not_swept_into_review_for_lacking_a_route(tmp_path):
    """--keep must beat the no-route rule, or the 'leave it' files all move to _review."""
    src = tmp_path / "sfx"
    src.mkdir()
    (src / "boom.wav").write_bytes(b"x")
    rows = asort.plan([_rec(src / "boom.wav", "sfx", 0.99, dur=500.0)],
                      {"music": tmp_path / "m"}, min_conf=0.6,
                      review_root=tmp_path / "_review", keep={"sfx"})
    assert rows[0]["action"] == "leave"
    assert rows[0]["review"] is False


def test_apply_does_not_move_files_marked_leave(tmp_path):
    src, music = tmp_path / "sfx", tmp_path / "music"
    src.mkdir()
    (src / "sting.wav").write_bytes(b"keepme")
    (src / "album.mp3").write_bytes(b"moveme")
    rows = asort.plan([_rec(src / "sting.wav", "music", 0.99, dur=3.0),
                       _rec(src / "album.mp3", "music", 0.99, dur=3600.0)],
                      {"music": music}, min_conf=0.6, min_dur=120)
    stats = asort.apply(rows, log_path=tmp_path / "log.jsonl")

    assert stats["moved"] == 1 and stats["left"] == 1
    assert (src / "sting.wav").read_bytes() == b"keepme"    # untouched
    assert (music / "album.mp3").read_bytes() == b"moveme"
    logged = [l for l in (tmp_path / "log.jsonl").read_text(encoding="utf-8").splitlines() if l]
    assert len(logged) == 1                                  # nothing logged for a leave


# ── companions: the sidecar must not be left behind ─────────────────────────
def test_source_directory_is_enumerated_once_not_once_per_file(tmp_path, monkeypatch):
    """Enumerate the source directory once, not once per row.

    NOTE: an earlier version of this docstring blamed a Windows enumeration race for 28
    orphaned sidecars in a real run. That was wrong — those files lived in a `_dupes`
    subfolder while their transcripts sat in the parent. The invariant is still worth
    pinning: per-row listing makes a 17k-file folder quadratic, and re-listing a
    directory you are actively modifying is a hazard worth not having.
    """
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir()
    recs = []
    for i in range(25):
        (src / f"clip{i}.m4a").write_bytes(b"a")
        (src / f"clip{i}.md").write_text("t", encoding="utf-8")
        recs.append(_rec(src / f"clip{i}.m4a", "music", 0.9))

    real_iterdir = Path.iterdir
    calls = Counter()

    def counting_iterdir(self):
        calls[str(self)] += 1
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", counting_iterdir)
    rows = asort.plan(recs, {"music": dest}, min_conf=0.6)
    stats = asort.apply(rows, log_path=tmp_path / "log.jsonl")

    assert calls[str(src)] == 1, f"listed the source dir {calls[str(src)]} times"
    assert stats["moved"] == 25 and stats["companions"] == 25
    assert not list(src.glob("*.md")), "sidecars left behind"


def test_every_sidecar_follows_its_audio_across_many_files(tmp_path):
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir()
    recs = []
    for i in range(40):
        (src / f"t{i}.m4a").write_bytes(b"a")
        (src / f"t{i}.md").write_text(f"transcript {i}", encoding="utf-8")
        recs.append(_rec(src / f"t{i}.m4a", "music", 0.9))
    asort.apply(asort.plan(recs, {"music": dest}, min_conf=0.6),
                log_path=tmp_path / "log.jsonl")
    for i in range(40):
        assert (dest / f"t{i}.md").read_text(encoding="utf-8") == f"transcript {i}"


def test_a_same_stem_audio_file_is_not_treated_as_a_sidecar(tmp_path):
    """`song.mp3` and `song.m4a` are two work items; one must not drag the other."""
    src, music, voice = tmp_path / "s", tmp_path / "m", tmp_path / "v"
    src.mkdir()
    (src / "song.m4a").write_bytes(b"aa")
    (src / "song.mp3").write_bytes(b"bb")
    (src / "song.md").write_text("t", encoding="utf-8")
    rows = asort.plan([_rec(src / "song.m4a", "music", 0.9),
                       _rec(src / "song.mp3", "voice", 0.9)],
                      {"music": music, "voice": voice}, min_conf=0.6)
    asort.apply(rows, log_path=tmp_path / "log.jsonl")

    assert (music / "song.m4a").exists()
    assert (voice / "song.mp3").exists(), "the .mp3 was dragged along as a sidecar"
    assert (music / "song.md").exists()      # the real sidecar went with the first


def test_a_file_already_at_its_destination_is_left_alone(tmp_path):
    """Re-running a sort must be a no-op, not a rename to 'name (2).ext'.

    `_unique` sees the destination occupied — by this very file — so without an
    explicit check the file is renamed on every run. It also bites when a tree
    contains its own destination (Production/ contains Video Edit Music/).
    """
    music = tmp_path / "music"
    music.mkdir()
    f = music / "song.mp3"
    f.write_bytes(b"audio")
    rows = asort.plan([_rec(f, "music", 0.95)], {"music": music}, min_conf=0.6)

    assert rows[0]["action"] == "leave"
    assert rows[0]["reason"] == "already filed here"
    stats = asort.apply(rows, log_path=tmp_path / "log.jsonl")
    assert stats.get("moved", 0) == 0
    assert f.read_bytes() == b"audio"
    assert not (music / "song (2).mp3").exists()


def test_sorting_a_tree_that_contains_its_own_destination_is_stable(tmp_path):
    """Production/ holding Video Edit Music/ must not churn on every sweep."""
    root = tmp_path / "Production"
    dest = root / "Video Edit Music"
    dest.mkdir(parents=True)
    (root / "loose.mp3").write_bytes(b"a")
    (dest / "already.mp3").write_bytes(b"b")
    recs = [_rec(root / "loose.mp3", "music", 0.9), _rec(dest / "already.mp3", "music", 0.9)]

    rows = asort.plan(recs, {"music": dest}, min_conf=0.6)
    asort.apply(rows, log_path=tmp_path / "log.jsonl")
    assert sorted(p.name for p in dest.iterdir()) == ["already.mp3", "loose.mp3"]

    # second sweep: nothing left to do, nothing renamed
    rows2 = asort.plan([_rec(dest / "already.mp3", "music", 0.9),
                        _rec(dest / "loose.mp3", "music", 0.9)], {"music": dest}, min_conf=0.6)
    assert all(r["action"] == "leave" for r in rows2)
    asort.apply(rows2, log_path=tmp_path / "log.jsonl")
    assert sorted(p.name for p in dest.iterdir()) == ["already.mp3", "loose.mp3"]
