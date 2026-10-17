"""``navig text channel sync`` — the positional master → scripts sync.

The fixtures are miniature channel workspaces: the same module shapes the real
channel ships (EPISODES / PAGES / LORE / POSTS / TX_POSTS), small enough to assert
byte-level source surgery. What is pinned here is the safety contract: a swap that
cannot find EXACTLY ONE occurrence is reported, never written; a dry run writes
nothing; ``--apply`` is idempotent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from navig_text.channel.parse import MasterError, parse_master
from navig_text.channel.sync import Syncer, pyrepr, swap_lit
from navig_text.cli import app as standalone_app
from navig_text.commands.text import text_app

runner = CliRunner()

MASTER = """# ALL TEXTS — test channel

Rules that must survive a rewrite.

---

## 1 · EPISODES (kinetic videos) — spoken lines, EN / RU / FR

### demo — TX 001 · end card: en “Find your five.” / ru “Найди своих.”

**demo-beat-01** (plate `p1`, pause 0.25s)

- [en] New EN line one.
- [ru] Новая строка.

**demo-beat-02** (plate `—`, pause 0.6s)

- [en] New EN line two.
- [ru] Новая строка два.

## 2 · TRANSMISSIONS

### transmission-001 — visual: {"plate": "x"}

- [en][head] New head EN
- [en][body] New body EN
- [ru][head] Новый заголовок
- [ru][body] Новый текст

## 3 · LORE TEXT POSTS (Telegram EN / VK RU)

**lore-alpha**

- [en] Alpha lore changed.
- [ru] Лор альфа изменён.

## 4 · POSTS

**post-one**

- [en][title] New title
- [en][caption] New caption
- [en][tags] #new #tags

## 5 · TRANSMISSION post lines (VK/Telegram)

- [tx-001][en] New tx line
- [tx-001][ru] Новая строка тx
"""

EPISODES_PY = '''"""channel episodes."""
EPISODES = {
    "demo": {
        "end": {"en": "Find your five.", "ru": "Найди своих."},
        "beats": [
            {"plate": "p1", "pause": 0.25, "say": {"en": "Old EN line one.", "ru": "Старая строка."}},
            {"plate": "—", "pause": 0.6, "say": {"en": "Old EN line two.", "ru": "Старая строка два."}},
        ],
    },
}
'''

TRANSMISSION_PY = '''"""channel bulletins."""
PAGES = {
    "001": {
        "en": {"head": "Old head EN", "sections": [("body", "Old body EN")]},
        "ru": {"head": "Старый заголовок", "sections": [("body", "Старый текст")]},
    },
}
'''

LORE_TEXTS_PY = '''"""channel lore."""
LORE = [
    {"id": "alpha", "en": "Alpha lore EN.", "ru": "Лор альфа."},
]
'''

PUBLISH_KIT_PY = '''"""channel posts."""
POSTS = {
    "one": {
        "title": {"en": "Old title"},
        "caption": {"en": "Old caption"},
        "tags": {"en": ["old", "tags"]},
    },
}
TX_POSTS = {"001": {"en": "Old tx line", "ru": "Старая строка тx"}}
'''


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """A complete miniature channel workspace + its master file."""
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "ALL-TEXTS-rev6.md").write_text(MASTER, encoding="utf-8")
    (tmp_path / "episodes.py").write_text(EPISODES_PY, encoding="utf-8", newline="\n")
    (tmp_path / "transmission.py").write_text(
        TRANSMISSION_PY, encoding="utf-8", newline="\n"
    )
    (tmp_path / "lore_texts.py").write_text(LORE_TEXTS_PY, encoding="utf-8", newline="\n")
    (tmp_path / "publish_kit.py").write_text(
        PUBLISH_KIT_PY, encoding="utf-8", newline="\n"
    )
    return tmp_path


def _sync(ws: Path, apply: bool = False, only: str | None = None, master: Path | None = None):
    path = master or (ws / "out" / "ALL-TEXTS-rev6.md")
    parsed = parse_master(path)
    return Syncer(ws, parsed, path, apply=apply, only=only).run()


# ── parse ─────────────────────────────────────────────────────────────────────
def test_parse_reads_every_section(workspace: Path):
    m = parse_master(workspace / "out" / "ALL-TEXTS-rev6.md")
    assert list(m.episodes) == ["demo"]
    assert len(m.episodes["demo"]["beats"]) == 2
    assert m.episodes["demo"]["beats"][0]["lines"]["en"] == "New EN line one."
    assert m.episodes["demo"]["end"]["en"] == "Find your five."
    assert m.tx["001"]["en"]["head"] == "New head EN"
    assert m.tx["001"]["ru"]["sections"] == [("body", "Новый текст")]
    assert m.lore["alpha"]["en"] == "Alpha lore changed."
    assert m.posts["one"]["tags"]["en"] == "#new #tags"
    assert m.txlines["001"]["en"] == "New tx line"


def test_parse_missing_file_raises(tmp_path: Path):
    with pytest.raises(MasterError):
        parse_master(tmp_path / "nope.md")


def test_parse_non_master_raises(tmp_path: Path):
    other = tmp_path / "notes.md"
    other.write_text("just notes", encoding="utf-8")
    with pytest.raises(MasterError):
        parse_master(other)


# ── dry run writes nothing ────────────────────────────────────────────────────
def test_dry_run_reports_but_never_writes(workspace: Path):
    before = (workspace / "episodes.py").read_text(encoding="utf-8")
    report = _sync(workspace, apply=False)
    assert (workspace / "episodes.py").read_text(encoding="utf-8") == before
    assert report.applied is False
    assert any(ln.strip() == "(dry) episodes.py" for ln in report.lines)
    assert report.summary().startswith("DRY —")
    # a dry run knows what apply WOULD touch
    assert "episodes.py" in report.pending


def test_dry_run_header_counts_sections(workspace: Path):
    report = _sync(workspace)
    assert report.header == (
        "rev6: 1 episodes, 1 transmissions, 1 lore, 1 posts, 1 tx lines"
    )


# ── apply: values land, idempotence holds ─────────────────────────────────────
def test_apply_updates_all_five_targets(workspace: Path):
    report = _sync(workspace, apply=True)
    assert report.problems == 0
    episodes = (workspace / "episodes.py").read_text(encoding="utf-8")
    transmission = (workspace / "transmission.py").read_text(encoding="utf-8")
    lore = (workspace / "lore_texts.py").read_text(encoding="utf-8")
    posts = (workspace / "publish_kit.py").read_text(encoding="utf-8")
    assert any(ln.strip() == "wrote episodes.py" for ln in report.lines)
    assert "Old EN line one." not in episodes
    assert "New EN line one." in episodes
    assert "Old head EN" not in transmission
    assert "New head EN" in transmission
    assert "Alpha lore changed." in lore
    assert "Old tx line" not in posts
    assert "New tx line" in posts
    # compiled clean
    compile(episodes, "episodes.py", "exec")
    compile(transmission, "transmission.py", "exec")
    compile(lore, "lore_texts.py", "exec")
    compile(posts, "publish_kit.py", "exec")


def test_apply_is_idempotent(workspace: Path):
    first = _sync(workspace, apply=True)
    assert first.problems == 0
    snapshots = {
        f: (workspace / f).read_text(encoding="utf-8")
        for f in ("episodes.py", "transmission.py", "lore_texts.py", "publish_kit.py")
    }
    second = _sync(workspace, apply=True)
    # every value already matches the master → nothing pending, nothing rewritten
    assert second.problems == 0
    assert second.pending == []
    assert not [ln for ln in second.lines if ln.strip().startswith("wrote")]
    for f, text in snapshots.items():
        assert (workspace / f).read_text(encoding="utf-8") == text


def test_lore_is_a_flat_list_after_apply(workspace: Path):
    """Regression: the old script nested LORE as its own first element (double-list)."""
    from navig_text.workspace import load_workspace_module

    _sync(workspace, apply=True)
    LT = load_workspace_module(workspace, "lore_texts")
    assert isinstance(LT.LORE, list)
    assert all(isinstance(e, dict) for e in LT.LORE), "LORE must be flat dicts"
    assert [e["id"] for e in LT.LORE] == ["alpha"]
    assert LT.LORE[0]["en"] == "Alpha lore changed."


def test_beat_count_mismatch_is_reported_not_written(workspace: Path):
    # master gains a beat the scripts do not have — inserted INSIDE section 1
    master = (workspace / "out" / "ALL-TEXTS-rev6.md").read_text(encoding="utf-8")
    extra = "\n**demo-beat-03** (plate `p3`, pause 0.1s)\n\n- [en] Third.\n- [ru] Третья.\n"
    master = master.replace("\n## 2 ·", extra + "\n## 2 ·")
    path = workspace / "out" / "extra.md"
    path.write_text(master, encoding="utf-8")
    before = (workspace / "episodes.py").read_text(encoding="utf-8")
    report = _sync(workspace, apply=True, master=path)
    assert report.problems == 1
    # label comes from the master filename: extra.md → "extra"
    assert "beat count 2 vs extra 3" in "\n".join(report.lines)
    # the episode block was skipped entirely — no partial write of that file
    assert (workspace / "episodes.py").read_text(encoding="utf-8") == before


# ── swap_lit safety ───────────────────────────────────────────────────────────
def test_swap_lit_needs_exactly_one_occurrence():
    report_lines: list[str] = []
    stats = {"ok": 0, "same": 0}
    # zero occurrences → reported
    out = swap_lit('x = "nope"', "absent", "new", "s", "f.py", report_lines, stats)
    assert out == 'x = "nope"'
    assert stats["ok"] == 0
    assert "occurrences=0" in report_lines[0]
    # two occurrences → reported, not written
    report_lines.clear()
    src = 'a = "dup"\nb = "dup"'
    out = swap_lit(src, "dup", "new", "s", "f.py", report_lines, stats)
    assert out == src
    assert "occurrences=2" in report_lines[0]
    # exactly one → swapped
    report_lines.clear()
    out = swap_lit('a = "one"', "one", "two", "s", "f.py", report_lines, stats)
    assert out == 'a = "two"'
    assert stats["ok"] == 1
    # identical values count as same, not a problem
    report_lines.clear()
    out = swap_lit('a = "same"', "same", "same", "s", "f.py", report_lines, stats)
    assert out == 'a = "same"'
    assert stats["same"] == 1
    assert not report_lines


def test_swap_lit_escapes_quotes_and_newlines():
    report_lines: list[str] = []
    stats = {"ok": 0, "same": 0}
    src = 'a = "say \\"hi\\""'
    out = swap_lit(src, 'say "hi"', "say 'there'", "s", "f.py", report_lines, stats)
    assert out == "a = \"say 'there'\""


def test_pyrepr_keeps_tuples_and_multiline():
    assert pyrepr(("a",)) == '("a",)'
    assert pyrepr([]) == "[]"
    assert pyrepr({}) == "{}"
    assert pyrepr("x\ny") == '"x\\ny"'
    long = pyrepr(["word " * 20])  # >60 chars → multiline
    assert "\n" in long


# ── --only ────────────────────────────────────────────────────────────────────
def test_only_narrows_to_one_target(workspace: Path):
    report = _sync(workspace, apply=True, only="lore")
    assert (workspace / "episodes.py").read_text(encoding="utf-8") == EPISODES_PY
    assert not [ln for ln in report.lines if "episodes.py" in ln]


def test_only_rejects_unknown_target(workspace: Path):
    with pytest.raises(ValueError):
        _sync(workspace, only="everything")


def test_only_txlines_skips_posts(workspace: Path):
    # txlines-only: the POSTS title must NOT be swapped, the tx line must
    new_master = MASTER.replace("New tx line", "Newer tx line")
    path = workspace / "out" / "ALL-TEXTS-rev6.md"
    path.write_text(new_master, encoding="utf-8")
    _sync(workspace, apply=True, only="txlines")
    posts = (workspace / "publish_kit.py").read_text(encoding="utf-8")
    assert '"Old title"' in posts  # untouched
    assert "Newer tx line" in posts


# ── CLI ───────────────────────────────────────────────────────────────────────
def test_cli_sync_dry(workspace: Path):
    result = runner.invoke(
        standalone_app,
        ["channel", "sync", "--workspace", str(workspace)],
    )
    assert result.exit_code == 0, result.output
    assert "DRY — swapped:" in result.output


def test_cli_sync_check_pending_exits_1(workspace: Path):
    # master differs from scripts → --check must fail
    path = workspace / "out" / "ALL-TEXTS-rev6.md"
    path.write_text(MASTER.replace("New EN line one.", "Newer EN line one."), encoding="utf-8")
    result = runner.invoke(
        standalone_app, ["channel", "sync", "--workspace", str(workspace), "--check"]
    )
    assert result.exit_code == 1, result.output


def test_cli_sync_apply_then_check_clean(workspace: Path):
    applied = runner.invoke(
        standalone_app, ["channel", "sync", "--workspace", str(workspace), "--apply"]
    )
    assert applied.exit_code == 0, applied.output
    checked = runner.invoke(
        standalone_app, ["channel", "sync", "--workspace", str(workspace), "--check"]
    )
    assert checked.exit_code == 0, checked.output


def test_cli_sync_apply_check_conflict(workspace: Path):
    result = runner.invoke(
        standalone_app,
        ["channel", "sync", "--workspace", str(workspace), "--apply", "--check"],
    )
    assert result.exit_code == 2


def test_cli_missing_workspace_exits_2(tmp_path: Path):
    result = runner.invoke(
        standalone_app,
        ["channel", "sync", "--workspace", str(tmp_path / "absent")],
    )
    assert result.exit_code == 2


def test_cli_bad_only_exits_2(workspace: Path):
    result = runner.invoke(
        standalone_app,
        ["channel", "sync", "--workspace", str(workspace), "--only", "nope"],
    )
    assert result.exit_code == 2, result.output


def test_cli_json_output(workspace: Path):
    result = runner.invoke(
        standalone_app,
        ["channel", "sync", "--workspace", str(workspace), "--json"],
    )
    assert result.exit_code == 0, result.output
    import json

    payload = json.loads(result.output)
    assert payload["applied"] is False
    assert payload["problems"] == 0


def test_cli_on_text_app_mounts_channel():
    """`navig text channel …` must exist on the group navig mounts."""
    from typer.main import get_command

    import click

    root = get_command(text_app)
    assert isinstance(root, click.Group)
    assert root.get_command(click.Context(root), "channel") is not None
