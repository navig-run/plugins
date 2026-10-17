"""Article drafts, series stats, localized narration plans, copy-vs-footage QA."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from navig_pipeline import draft as D
from navig_pipeline import series as S
from navig_pipeline.localize import LocalizeError, localize_plan
from navig_pipeline.qa import lint_copy

PATHS = {"navig repo lock status", "navig repo new", "navig run"}
ARTICLE = "Two sessions shared one server. `navig repo lock status` shows the holder; locks expire after 60 minutes."


def _ask(payload: dict):
    return lambda prompt: json.dumps(payload)


GOOD = {"hook": {"title": "Five agents, *one repo*", "sub": "Locks, not luck."},
        "intro": "Two sessions shared one server.",
        "beats": [{"command": "navig repo lock status", "caption": "Who holds *main*", "line": "One command shows the holder."},
                  {"command": "navig repo explode --all", "caption": "x", "line": "y"}],
        "outro": "Locks expire after 60 minutes.",
        "posting": {"caption": "Locks expire after 90 minutes.", "hashtags": ["#ai"]}}


def test_a_made_up_command_is_dropped_and_an_unsourced_number_flagged() -> None:
    d = D.draft(ARTICLE, _ask(GOOD), PATHS)
    assert [b["command"] for b in d.beats] == ["navig repo lock status"]
    assert d.invalid_commands == ["navig repo explode --all"]
    assert d.unsourced_numbers == ["90"]          # the article says 60


def test_resolves_by_leading_command_words() -> None:
    assert D.resolves("navig repo lock status --json", PATHS)
    assert not D.resolves("navig repo", PATHS) and not D.resolves("git status", PATHS)


def test_a_draft_writes_a_folder_episode_build_understands(tmp_path: Path) -> None:
    d = D.draft(ARTICLE, _ask(GOOD), PATHS)
    D.write(d, tmp_path / "e08", title="Five agents")
    plan = json.loads((tmp_path / "e08" / "rebuild.json").read_text(encoding="utf-8"))
    scene = json.loads((tmp_path / "e08" / "scene.json").read_text(encoding="utf-8"))
    assert [s["type"] for s in scene["segments"]] == ["hook", "terminal", "outro"]
    assert scene["segments"][1]["cast"] == "1-repo-lock-status.cast"
    assert len(plan["segs"]) == len(scene["segments"]) and plan["speech_map"] == [None, 1, None]
    capture = (tmp_path / "e08" / "CAPTURE.md").read_text(encoding="utf-8")
    assert "navig pipeline cast -o 1-repo-lock-status.cast --cols 44 -- navig repo lock status" in capture
    assert "navig repo explode" in capture and "90" in capture   # dropped + flagged, not hidden


def test_non_json_or_empty_drafts_are_refused() -> None:
    with pytest.raises(D.DraftError, match="JSON"):
        D.draft(ARTICLE, lambda p: "Sure! Here is a script", PATHS)
    replies = iter(["", json.dumps(GOOD)])                       # empty once, then fine
    assert D.draft(ARTICLE, lambda p: next(replies), PATHS).beats
    with pytest.raises(D.DraftError, match="beats"):
        D.draft(ARTICLE, _ask({**GOOD, "beats": []}), PATHS)


class FakeStore:
    def __init__(self):
        self.rows: dict[tuple, int] = {}

    def upsert_metric(self, *, campaign, network, post_id, metric, value, source):
        self.rows[(campaign, network, post_id, metric)] = value          # replace-latest

    def post_metrics(self, campaign, network=None):
        out: dict = {}
        for (c, n, p, m), v in self.rows.items():
            if c == campaign and (network is None or n == network):
                out.setdefault(p, {})[m] = v
        return out


def _episode(series: Path, name: str, hook: float) -> Path:
    d = series / name
    (d / "_vo").mkdir(parents=True)
    (d / "scene.json").write_text("{}", encoding="utf-8")
    (d / "clip-v2.md").write_text(f"---\naudio: _vo/track-final.mp3\nsections: [0, {hook}, 10.0]\n---\n", encoding="utf-8")
    (d / "_vo" / "beats.json").write_text(json.dumps({"lines": [{"text": "one two three four", "dur": 2.0, "end": 20.0}]}),
                                          encoding="utf-8")
    return d


def test_stats_are_snapshots_joined_with_what_the_cut_is(tmp_path: Path) -> None:
    store = FakeStore()
    e07 = _episode(tmp_path, "e07", 3.52)
    _episode(tmp_path, "e06", 2.82)
    S.record(e07, {"views": 900, "avg_watch_ms": 10000}, store=store)
    S.record(e07, {"views": 1200}, store=store)                    # re-entry replaces, never sums
    rows = {r["episode"]: r for r in S.table(tmp_path, store=store)}
    assert rows["e07"]["views"] == 1200 and rows["e07"]["hook_s"] == 3.52
    assert rows["e07"]["watch_ratio"] == 0.5 and rows["e07"]["wps"] == 2.0
    assert "views" not in rows["e06"]                               # no numbers yet, still listed
    with pytest.raises(S.SeriesError):
        S.record(e07, {"views": -1}, store=store)


def test_a_localized_plan_keeps_cuts_and_voices_in_its_own_folders() -> None:
    cfg = {"lines": [["n1", "daniel", "Hello.", 0.5], ["k1", "brian", "Hello.", 0.0]],
           "segs": [{"line": "n1", "hold": 0.4}, {}], "speech_map": [None, 1], "length": 10, "bed": "b",
           "scene": "scene-v2.json", "clip": "clip-v2.md", "updates": [{"i": 1, "set": {"pace": 5}}]}
    seen = []
    plan, counts = localize_plan(cfg, "fr", lambda t, lang: seen.extend(t) or ["Bonjour."] * len(t))
    assert seen == ["Hello."]                                        # a repeated line is translated once
    assert plan["lines"] == [["n1", "daniel", "Bonjour.", 0.5], ["k1", "brian", "Bonjour.", 0.0]]
    assert (plan["scene"], plan["clip"], plan["vo_dir"], plan["lang"]) == (
        "scene-v2.fr.json", "clip-v2-fr.md", "_vo-fr", "fr")
    assert plan["segs"] == cfg["segs"] and plan["updates"] == [] and cfg["lines"][0][2] == "Hello."
    with pytest.raises(LocalizeError):
        localize_plan(cfg, "fr", lambda t, lang: [])


def test_copy_numbers_must_be_in_the_footage() -> None:
    scene = {"segments": [{"type": "hook", "title": "Quest 3", "sub": "x"},
                          {"type": "terminal", "caption": "7 high-risk"}]}
    casts = ["Scanned \x1b[36m4\x1b[0m installed extensions · Quest 3 · 3,072 px"]
    bad = lint_copy({"caption": "840 extensions scanned"}, scene, casts)          # E05's shipped claim
    assert [(f.level, f.rule) for f in bad] == [("error", "unsupported-number")]
    assert lint_copy({"caption": "4 extensions, 3072 px, 7 high-risk, `navig x 99`"}, scene, casts) == []


def test_documentation_shapes_are_not_capturable_commands() -> None:
    # both offered by the first real draft (article 02); each passed the prefix check
    paths = PATHS | {"navig host lock status"}
    d = D.draft(ARTICLE, _ask({**GOOD, "beats": [
        {"command": "navig host lock status | acquire | release", "caption": "c", "line": "l"},
        {"command": "navig repo new agent-<n>", "caption": "c", "line": "l"},
        {"command": "navig repo lock status", "caption": "c", "line": "l"}]}), paths)
    assert [b["command"] for b in d.beats] == ["navig repo lock status"]
    assert len(d.invalid_commands) == 2


def test_state_changing_captures_are_flagged_and_hashtags_cleaned(tmp_path: Path) -> None:
    assert D.read_only("navig repo conflicts") and not D.read_only("navig repo new demo")
    assert D.hashtag("#ai-agents") == "#aiagents" and D.hashtag("#") == ""
    d = D.draft(ARTICLE, _ask({**GOOD, "beats": [{"command": "navig repo new demo", "caption": "c", "line": "l"}],
                               "posting": {"caption": "x", "hashtags": ["#ai-agents", "navig"]}}),
                PATHS)
    D.write(d, tmp_path / "e", title="t")
    assert "may change state" in (tmp_path / "e" / "CAPTURE.md").read_text(encoding="utf-8")
    assert json.loads((tmp_path / "e" / "posting.json").read_text(encoding="utf-8"))["hashtags"] == ["#navig", "#aiagents"]
