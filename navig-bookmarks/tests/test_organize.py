"""The organize planner: grouping, batching, validation, failsafe — all without a real AI."""

from __future__ import annotations

import json
import re

import pytest

from navig_bookmarks import organize as org
from navig_bookmarks.models import Bookmark


def _bm(i: int, url: str, title: str = "", folder: str = "") -> Bookmark:
    return Bookmark(id=f"b{i:03d}", url=url, title=title or f"page {i}", folder_path=folder)


def _ids_in(user_prompt: str) -> list[str]:
    return re.findall(r"^(\S+) \| ", user_prompt, flags=re.M)


# ── grouping and batching ───────────────────────────────────────────────────────


def test_a_busy_site_becomes_one_unit_but_platforms_never_do() -> None:
    bms = [_bm(i, f"https://docs.python.org/3/p{i}") for i in range(6)]
    bms += [_bm(10 + i, f"https://github.com/o/r{i}") for i in range(6)]
    bms.append(_bm(99, "https://example.com/solo"))
    units = org.build_units(bms, group_min=5)
    keys = [u.key for u in units]
    assert "g:python.org" in keys
    assert next(u for u in units if u.key == "g:python.org").members == [f"b{i:03d}" for i in range(6)]
    assert not any(k == "g:github.com" for k in keys)  # 6 repos are 6 subjects
    assert sum(len(u.members) for u in units) == len(bms)
    assert all(k.startswith("g:") or k.startswith("b") for k in keys)


def test_group_min_zero_disables_grouping() -> None:
    bms = [_bm(i, f"https://docs.python.org/{i}") for i in range(8)]
    assert all(not u.key.startswith("g:") for u in org.build_units(bms, group_min=0))


def test_batches_respect_size_and_count() -> None:
    units = [org.Unit(f"b{i}", [f"b{i}"], "x" * 100) for i in range(50)]
    batches = org.batch_units(units, max_chars=1000, max_units=150)
    assert all(sum(len(u.text) + len(u.key) + 4 for u in b) <= 1000 for b in batches)
    assert [u.key for b in batches for u in b] == [u.key for u in units]
    assert len(org.batch_units(units, max_chars=10**9, max_units=7)[0]) == 7


# ── the plan ────────────────────────────────────────────────────────────────────


def _fake_ai(folder_for, *, drop_first_time=(), invent=(), truncate_once=False):
    """A generate() stand-in that answers from *folder_for(id)* and can misbehave."""
    state = {"calls": 0, "dropped": set()}

    def gen(system: str, user: str, max_tokens: int) -> str:
        state["calls"] += 1
        rows = []
        for key in _ids_in(user.split("Bookmarks (id")[1]):
            if key in drop_first_time and key not in state["dropped"]:
                state["dropped"].add(key)
                continue
            rows.append({"id": key, "folder": folder_for(key)})
        rows += [{"id": k, "folder": "Made Up"} for k in invent]
        text = json.dumps({"assignments": rows})
        if truncate_once and state["calls"] == 1:
            text = text[: len(text) * 2 // 3]
        return text

    gen.state = state
    return gen


def test_plan_expands_groups_and_retries_skipped_ids() -> None:
    bms = [_bm(i, f"https://docs.python.org/{i}") for i in range(5)] + [_bm(9, "https://example.com/x")]
    gen = _fake_ai(lambda k: "Dev/Python" if k.startswith("g:") else "Misc Stuff", drop_first_time={"b009"})
    res = org.plan_organize(bms, gen, group_min=5)
    assert res.missing == [] and res.invented == []
    assert res.plan == {**{f"b{i:03d}": "Dev/Python" for i in range(5)}, "b009": "Misc Stuff"}
    assert res.calls == 2  # one retry, for the skipped id only


def test_invented_ids_are_reported_and_fail_validation() -> None:
    bms = [_bm(1, "https://a.example"), _bm(2, "https://b.example")]
    res = org.plan_organize(bms, _fake_ai(lambda k: "Reading", invent=("zzz999",)), group_min=0)
    assert res.invented == ["zzz999"]
    errors = org.validate_plan(res.plan, [b.id for b in bms], invented=res.invented)
    assert any("unknown id" in e for e in errors)


def test_a_truncated_reply_is_repaired_then_completed() -> None:
    bms = [_bm(i, f"https://site{i}.example") for i in range(12)]
    gen = _fake_ai(lambda k: "Reading/Later", truncate_once=True)
    res = org.plan_organize(bms, gen, group_min=0)
    assert res.missing == [] and len(res.plan) == 12 and gen.state["calls"] == 2


def test_ids_still_missing_after_the_retry_are_reported() -> None:
    bms = [_bm(1, "https://a.example"), _bm(2, "https://b.example")]

    def gen(system, user, max_tokens):
        return json.dumps({"assignments": [{"id": "b001", "folder": "A"}]})

    res = org.plan_organize(bms, gen, group_min=0)
    assert res.missing == ["b002"]
    assert any("got no folder" in e for e in org.validate_plan(res.plan, ["b001", "b002"]))


def test_ai_errors_propagate() -> None:
    def gen(system, user, max_tokens):
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError):
        org.plan_organize([_bm(1, "https://a.example")], gen)


def test_known_folders_flow_into_later_batches() -> None:
    bms = [_bm(i, f"https://site{i}.example", title="t" * 80) for i in range(40)]
    prompts = []

    def gen(system, user, max_tokens):
        prompts.append(user)
        return json.dumps({"assignments": [{"id": k, "folder": f"Topic/{k}"} for k in _ids_in(user.split("Bookmarks (id")[1])]})

    org.plan_organize(bms, gen, existing_folders=["Existing/One"], group_min=0, max_chars=2000)
    assert len(prompts) > 1
    assert "Existing/One" in prompts[0]
    assert "Topic/b000" in prompts[-1]  # a folder created in batch 1 is offered to the last batch


# ── validation, sanitising, failsafe, diff ──────────────────────────────────────


def test_sanitize_folder_cleans_and_clamps() -> None:
    assert org.sanitize_folder('  "Dev" /  Python  ') == ("Dev/Python", False)
    assert org.sanitize_folder("A > B > C > D") == ("A/B/C", True)
    assert org.sanitize_folder("A/B/C/D", max_depth=2) == ("A/B", True)
    assert org.sanitize_folder(" / ") == ("", False)


def test_an_existing_folder_with_a_slash_in_its_name_is_reused_exactly() -> None:
    """Found on a real run: the AI echoed `Dev/A/B testing` and a plain split cut the name."""
    stored = r"Bookmarks bar/Dev/A\/B testing"
    known = org.folder_lookup([stored])
    for echoed in ("Bookmarks bar/Dev/A/B testing", stored, "bookmarks bar/dev/a/b testing"):
        assert org.sanitize_folder(echoed, known=known) == (stored, False)
    # A new path still splits on "/", and an escaped slash stays inside its name.
    assert org.sanitize_folder(r"Work/A\/B tests") == (r"Work/A\/B tests", False)


def test_validate_plan_accepts_a_good_plan() -> None:
    assert org.validate_plan({"a": "X", "b": "X/Y/Z"}, ["a", "b"]) == []


def test_validate_plan_names_every_problem() -> None:
    errors = org.validate_plan({"a": "A/B/C/D", "b": "", "ghost": "X"}, ["a", "b", "c"])
    text = " ".join(errors)
    assert "got no folder: c" in text
    assert "unknown id(s) in the plan: ghost" in text
    assert "deeper than 3" in text
    assert "empty folder" in text


def test_failsafe_trips_above_twenty_percent_moves() -> None:
    current = {f"b{i}": "Old" for i in range(10)}
    two = {**current, "b0": "New", "b1": "New"}
    three = {**two, "b2": "New"}
    assert not org.failsafe(two, current).tripped  # exactly 20 %
    verdict = org.failsafe(three, current)
    assert verdict.tripped and verdict.moves == 3 and "30%" in verdict.reasons[0]


def test_failsafe_trips_above_a_thousand_changes() -> None:
    current = {f"b{i}": "Old" for i in range(20000)}
    plan = {**current, **{f"b{i}": f"F{i}" for i in range(600)}}  # 600 moves + 600 new folders
    verdict = org.failsafe(plan, current)
    assert verdict.ratio < 0.2 and verdict.changes == 1200 and verdict.tripped


def test_diff_rows() -> None:
    current = {"a": "Old", "b": "Old", "c": "Keep"}
    plan = {"a": "New", "b": "Old", "c": "Keep"}
    assert org.diff_rows(plan, current) == [("New", 0, 1), ("Old", 2, 1)]
