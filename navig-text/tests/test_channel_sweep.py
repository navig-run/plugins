"""``navig text channel sweep`` — literal terminology replacement, reported first.

The blast-radius rules are what matter: tokens (``{citiesOpen}``), hashtags
(``#citylife``) and code spans must survive a ``city → Zone`` sweep untouched,
because they are not the word "city".
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from navig_text.channel.sweep import Rule, sweep_file, sweep_rules
from navig_text.cli import app as standalone_app

runner = CliRunner()

SAMPLE = """- [en] {citiesOpen} cities are still dark. #citylife is a tag.
- [en] Name your city. See `city-block` in the card.
- [en] Cities became Zones already.
"""


def test_whole_word_sweep_skips_tokens_tags_and_code(tmp_path: Path):
    f = tmp_path / "master.md"
    f.write_text(SAMPLE, encoding="utf-8")
    result = sweep_file(f, _default(), apply=False)
    # dry: file untouched
    assert f.read_text(encoding="utf-8") == SAMPLE
    rendered = "\n".join(after for _, _, after in result.hits)
    assert "{citiesOpen}" in rendered  # token untouched
    assert "#citylife" in rendered  # tag untouched
    assert "`city-block`" in rendered  # code span untouched
    assert "cities are still dark" not in rendered.replace("{citiesOpen}", "")
    assert "Name your Zone" in rendered  # the plain word DID change
    assert "Zones became Zones" in rendered  # capitalized form swept too


def _default() -> list[Rule]:
    return [
        Rule("cities", "Zones"),
        Rule("city", "Zone"),
        Rule("Cities", "Zones"),
        Rule("City", "Zone"),
    ]


def test_apply_writes_and_is_idempotent(tmp_path: Path):
    f = tmp_path / "master.md"
    f.write_text(SAMPLE, encoding="utf-8")
    first = sweep_file(f, _default(), apply=True)
    assert first.changed
    after = f.read_text(encoding="utf-8")
    assert "{citiesOpen}" in after
    assert "#citylife" in after
    assert "`city-block`" in after
    assert "cities are still" not in after
    assert "Name your Zone" in after
    second = sweep_file(f, _default(), apply=True)
    assert not second.changed
    assert second.hits == []
    assert f.read_text(encoding="utf-8") == after


def test_dry_reports_without_writing(tmp_path: Path):
    f = tmp_path / "master.md"
    f.write_text(SAMPLE, encoding="utf-8")
    result = sweep_file(f, _default(), apply=False)
    assert result.hits
    assert result.changed is True  # would change
    assert result.applied is False
    assert f.read_text(encoding="utf-8") == SAMPLE
    assert "(dry)" in result.render()


def test_rules_from_channel_config(tmp_path: Path):
    cfg = {
        "sweep": {
            "rules": [
                {"find": "pigeons", "replace": "messengers"},
                {"find": "ketchup", "replace": "the red condiment", "whole_word": False},
            ]
        }
    }
    (tmp_path / "channel.json").write_text(json.dumps(cfg), encoding="utf-8")
    rules = sweep_rules(tmp_path)
    assert rules[0] == Rule("pigeons", "messengers", True)
    assert rules[1].whole_word is False


def test_scope_content_skips_operator_lines(tmp_path: Path):
    """Headers, rules and tables are operator-facing — only `- [lang]` lines sweep."""
    f = tmp_path / "master.md"
    f.write_text(
        "# Rules header mentions city.\n"
        "| Living City | LIV-ing SIT-ee |\n"
        "- [en] Name your city.\n",
        encoding="utf-8",
    )
    sweep_file(f, _default(), apply=True, scope="content")
    after = f.read_text(encoding="utf-8")
    assert "Rules header mentions city." in after  # untouched
    assert "Living City" in after  # table untouched
    assert "Name your Zone." in after  # content swept


def test_allow_phrase_skips_the_whole_line(tmp_path: Path):
    f = tmp_path / "master.md"
    f.write_text(
        "- [en] Cities became *Zones* already.\n"
        "- [en] A Zone is a city — on the Grid.\n"
        "- [en] Name your city.\n",
        encoding="utf-8",
    )
    result = sweep_file(
        f,
        _default(),
        apply=True,
        scope="content",
        allow=("Cities became", "is a city"),
    )
    after = f.read_text(encoding="utf-8")
    assert "Cities became *Zones*" in after  # allowed
    assert "is a city — on the Grid" in after  # allowed
    assert "Name your Zone." in after  # not allowed → swept
    assert len(result.hits) == 1


def test_scope_all_sweeps_every_line(tmp_path: Path):
    f = tmp_path / "master.md"
    f.write_text("# A city heading\n- [en] A city line.\n", encoding="utf-8")
    sweep_file(f, _default(), apply=True, scope="all")
    after = f.read_text(encoding="utf-8")
    assert "A Zone heading" in after
    assert "A Zone line." in after


def test_config_falls_back_to_defaults(tmp_path: Path):
    (tmp_path / "channel.json").write_text("{}", encoding="utf-8")
    assert sweep_rules(tmp_path) == [
        Rule("cities", "Zones"),
        Rule("city", "Zone"),
        Rule("Cities", "Zones"),
        Rule("City", "Zone"),
    ]


def test_cli_sweep_dry_then_apply(tmp_path: Path):
    ws = tmp_path
    (ws / "out").mkdir()
    f = ws / "out" / "ALL-TEXTS-rev6.md"
    f.write_text(SAMPLE, encoding="utf-8")

    dry = runner.invoke(standalone_app, ["channel", "sweep", "--workspace", str(ws)])
    assert dry.exit_code == 0, dry.output
    assert "hit(s)" in dry.output
    assert f.read_text(encoding="utf-8") == SAMPLE

    applied = runner.invoke(
        standalone_app, ["channel", "sweep", "--workspace", str(ws), "--apply"]
    )
    assert applied.exit_code == 0, applied.output
    assert "Name your Zone" in f.read_text(encoding="utf-8")


def test_cli_sweep_check_exits_1_when_pending(tmp_path: Path):
    ws = tmp_path
    (ws / "out").mkdir()
    f = ws / "out" / "ALL-TEXTS-rev6.md"
    f.write_text(SAMPLE, encoding="utf-8")
    result = runner.invoke(
        standalone_app, ["channel", "sweep", "--workspace", str(ws), "--check"]
    )
    assert result.exit_code == 1, result.output


def test_cli_sweep_check_clean_exits_0(tmp_path: Path):
    ws = tmp_path
    (ws / "out").mkdir()
    f = ws / "out" / "ALL-TEXTS-rev6.md"
    f.write_text("- [en] Zones only here.\n", encoding="utf-8")
    result = runner.invoke(
        standalone_app, ["channel", "sweep", "--workspace", str(ws), "--check"]
    )
    assert result.exit_code == 0, result.output


def test_cli_sweep_apply_check_conflict(tmp_path: Path):
    ws = tmp_path
    (ws / "out").mkdir()
    (ws / "out" / "ALL-TEXTS-rev6.md").write_text(SAMPLE, encoding="utf-8")
    result = runner.invoke(
        standalone_app,
        ["channel", "sweep", "--workspace", str(ws), "--apply", "--check"],
    )
    assert result.exit_code == 2
