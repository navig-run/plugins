"""``navig text channel lint`` — the master's own declared rules, plus the lore guard.

Each finding is pinned by a fixture that violates exactly one rule, so a lint that
stops finding it fails here rather than shipping a silent regression.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from navig_text.channel.lint import lint_master, lint_scripts
from navig_text.channel.parse import parse_master
from navig_text.cli import app as standalone_app

runner = CliRunner()

BASE_MASTER = """# ALL TEXTS — clean channel

---

## 1 · EPISODES (kinetic videos) — spoken lines, EN / RU / FR

### demo — TX 001 · end card: en “Go.” / ru “Иди.”

**demo-beat-01** (plate `p1`, pause 0.25s)

- [en] A clean line with *one red word*.
- [ru] Чистая строка с *одним красным*.

## 2 · TRANSMISSIONS

### transmission-001 — visual: {"plate": "x"}

- [en][head] Head is here
- [ru][head] Заголовок здесь

## 3 · LORE TEXT POSTS (Telegram EN / VK RU)

**lore-alpha**

- [en] Alpha lore.
- [ru] Лор альфа.

## 4 · POSTS

**post-one**

- [en][title] A title
- [en][caption] A caption
- [en][tags] #one #two

## 5 · TRANSMISSION post lines (VK/Telegram)

- [tx-001][en] A tx line
"""

EPISODES_PY = '''EPISODES = {
    "demo": {
        "end": {"en": "Go.", "ru": "Иди."},
        "beats": [
            {"plate": "p1", "pause": 0.25, "say": {"en": "A clean line with one red word.", "ru": "Чистая строка с одним красным."}},
        ],
    },
}
'''

LORE_TEXTS_PY = '''LORE = [
    {"id": "alpha", "en": "Alpha lore.", "ru": "Лор альфа."},
]
'''


CARDS_MASTER_TAIL = """
## 8 · ARCHIVE CARDS (panel 1920×1080 + mobile story 1080×1920) — EN / RU

**ST-001** · art: a test card

- [en][head] Card head *red*.
- [en][sub] Card sub.
- [ru][head] Заголовок.
- [ru][sub] Подзаголовок.

## 9 · CHANNEL FORMATS (statement videos, roll call) — EN only (no voice)

- [end-door-en] {daysLeft} days until the door opens
"""

STORY_KIT_PY = '''STORIES = [
    {"id": "001", "src": "x.png", "bias": (0.5, 0.5),
     "chip": "SCHEMA // ARCHIVE 001",
     "head": "Card head *red*.", "sub": "Card sub.",
     "ru_head": "Заголовок.", "ru_sub": "Подзаголовок."},
]
'''


def _ws(
    tmp_path: Path,
    master: str = BASE_MASTER,
    lore: str = LORE_TEXTS_PY,
    story_kit: str | None = "STORIES = []\n",
) -> Path:
    (tmp_path / "out").mkdir(exist_ok=True)
    (tmp_path / "out" / "ALL-TEXTS-rev6.md").write_text(master, encoding="utf-8")
    (tmp_path / "episodes.py").write_text(EPISODES_PY, encoding="utf-8", newline="\n")
    (tmp_path / "transmission.py").write_text("PAGES = {}\n", encoding="utf-8")
    (tmp_path / "lore_texts.py").write_text(lore, encoding="utf-8", newline="\n")
    (tmp_path / "publish_kit.py").write_text(
        "POSTS = {}\nTX_POSTS = {}\n", encoding="utf-8"
    )
    if story_kit is not None:
        (tmp_path / "story_kit.py").write_text(story_kit, encoding="utf-8", newline="\n")
    return tmp_path


def _lint(ws: Path, strict: bool = False):
    path = ws / "out" / "ALL-TEXTS-rev6.md"
    parsed = parse_master(path)
    return lint_master(path, parsed, strict=strict) + lint_scripts(ws, parsed)


# ── clean master passes ───────────────────────────────────────────────────────
def test_clean_master_has_no_errors(tmp_path: Path):
    ws = _ws(tmp_path)
    findings = _lint(ws)
    errors = [f for f in findings if f.severity == "error"]
    assert errors == [], [f.render() for f in errors]


def test_cli_lint_clean_exits_0(tmp_path: Path):
    ws = _ws(tmp_path)
    result = runner.invoke(
        standalone_app, ["channel", "lint", "--workspace", str(ws)]
    )
    assert result.exit_code == 0, result.output
    # a note (first-id canon) may print, but no ✗ and exit 0 = clean
    assert "✗" not in result.output


# ── each rule fires ───────────────────────────────────────────────────────────
def test_unpaired_asterisk_is_an_error(tmp_path: Path):
    master = BASE_MASTER.replace("*one red word*", "*one red word")
    ws = _ws(tmp_path, master=master)
    findings = _lint(ws)
    assert any(
        f.severity == "error" and "unpaired *" in f.message for f in findings
    ), [f.render() for f in findings]


def test_vendor_name_is_an_error(tmp_path: Path):
    master = BASE_MASTER.replace("A clean line", "Powered by Cloudflare")
    ws = _ws(tmp_path, master=master)
    findings = _lint(ws)
    assert any(
        f.severity == "error" and "vendor name" in f.message for f in findings
    ), [f.render() for f in findings]


def test_capitalized_role_word_is_a_note_and_strict_error(tmp_path: Path):
    master = BASE_MASTER.replace("A clean line", "The Citizen agrees")
    ws = _ws(tmp_path, master=master)
    soft = [f for f in _lint(ws) if "capitalized role word" in f.message]
    assert soft and soft[0].severity == "note"
    hard = [
        f
        for f in _lint(ws, strict=True)
        if "capitalized role word" in f.message and f.severity == "error"
    ]
    assert hard


def test_lowercase_world_term_is_an_error(tmp_path: Path):
    master = BASE_MASTER.replace(
        "- [ru] Чистая строка с *одним красным*.",
        "- [ru] Как устроена сетка внутри строки.",
    )
    ws = _ws(tmp_path, master=master)
    findings = _lint(ws)
    assert any(
        f.severity == "error" and "lost its capital" in f.message for f in findings
    ), [f.render() for f in findings]


def test_hashtags_and_tokens_are_not_prose(tmp_path: Path):
    """`#сетка` is a tag and `{citiesOpen}` a token — casing rules skip both."""
    master = BASE_MASTER.replace(
        "- [en][tags] #one #two",
        "- [en][tags] #сетка #Citizen #citiesOpen",
    ).replace(
        "- [ru] Чистая строка с *одним красным*.",
        "- [ru] Сетка держит {citiesOpen} узлов.",
    )
    ws = _ws(tmp_path, master=master)
    findings = _lint(ws)
    casing = [
        f
        for f in findings
        if "capitalized role word" in f.message or "lost its capital" in f.message
    ]
    assert casing == [], [f.render() for f in casing]


def test_duplicate_lore_id_in_master_is_an_error(tmp_path: Path):
    master = BASE_MASTER.replace(
        "## 4 · POSTS",
        "**lore-alpha**\n\n- [en] Again.\n- [ru] Опять.\n\n## 4 · POSTS",
    )
    ws = _ws(tmp_path, master=master)
    findings = _lint(ws)
    assert any(
        f.severity == "error" and "duplicate lore ids in master" in f.message
        for f in findings
    ), [f.render() for f in findings]


def test_beat_count_mismatch_is_an_error(tmp_path: Path):
    episodes_two = EPISODES_PY.replace(
        "        ],\n    },\n}",
        '            {"plate": "p2", "pause": 0.5, "say": {"en": "Second beat.", "ru": "Второй."}},\n        ],\n    },\n}',
    )
    ws = _ws(tmp_path)
    (ws / "episodes.py").write_text(episodes_two, encoding="utf-8", newline="\n")
    findings = _lint(ws)
    assert any(
        f.severity == "error" and "beat count" in f.message for f in findings
    ), [f.render() for f in findings]


def test_corrupted_lore_list_is_reported_not_crashed(tmp_path: Path):
    """The double-list bug's residue: LORE holds a nested list → lint must say so."""
    lore_bad = 'LORE = [\n    [\n        {"id": "alpha", "en": "x", "ru": "y"},\n    ],\n]\n'
    ws = _ws(tmp_path, lore=lore_bad)
    findings = _lint(ws)
    # lint_scripts imports lore_texts — a nested list must produce an error finding,
    # not an unhandled TypeError
    assert any(f.severity == "error" for f in findings), [f.render() for f in findings]


def test_master_entry_missing_from_scripts_is_an_error(tmp_path: Path):
    lore_empty = "LORE = []\n"
    ws = _ws(tmp_path, lore=lore_empty)
    findings = _lint(ws)
    assert any(
        f.severity == "error" and "not in lore_texts.py" in f.message
        for f in findings
    ), [f.render() for f in findings]


# ── story cards: master §8 vs story_kit.STORIES ──────────────────────────────
def _cards_ws(tmp_path: Path, *, master_tail: str = CARDS_MASTER_TAIL,
              kit: str = STORY_KIT_PY) -> Path:
    return _ws(tmp_path, master=BASE_MASTER + master_tail, story_kit=kit)


def test_cards_section_parses(tmp_path: Path):
    ws = _cards_ws(tmp_path)
    parsed = parse_master(ws / "out" / "ALL-TEXTS-rev6.md")
    assert "001" in parsed["cards"]
    assert parsed["cards"]["001"]["en"]["head"] == "Card head *red*."
    assert parsed["cards"]["001"]["ru"]["sub"] == "Подзаголовок."


def test_aligned_cards_have_no_findings(tmp_path: Path):
    findings = _lint(_cards_ws(tmp_path))
    assert not [f for f in findings if f.severity == "error"], [
        f.render() for f in findings
    ]


def test_card_head_drift_is_an_error(tmp_path: Path):
    kit = STORY_KIT_PY.replace("Card head *red*.", "Drifted head.")
    findings = _lint(_cards_ws(tmp_path, kit=kit))
    errs = [f for f in findings if f.severity == "error"]
    assert any("cards:ST-001 [en][head]" in f.where for f in errs), [
        f.render() for f in errs
    ]


def test_card_ru_drift_is_an_error(tmp_path: Path):
    kit = STORY_KIT_PY.replace("Подзаголовок.", "Другой.")
    findings = _lint(_cards_ws(tmp_path, kit=kit))
    assert any(
        f.severity == "error" and "cards:ST-001 [ru][sub]" in f.where
        for f in findings
    ), [f.render() for f in findings]


def test_card_in_master_missing_from_kit(tmp_path: Path):
    findings = _lint(_cards_ws(tmp_path, kit="STORIES = []\n"))
    assert any(
        f.severity == "error" and "not in story_kit.py" in f.message
        for f in findings
    ), [f.render() for f in findings]


def test_card_in_kit_missing_from_master(tmp_path: Path):
    # kit carries a second card the master never heard of
    kit = '''STORIES = [
    {"id": "001", "src": "x.png", "bias": (0.5, 0.5),
     "chip": "SCHEMA // ARCHIVE 001",
     "head": "Card head *red*.", "sub": "Card sub.",
     "ru_head": "Заголовок.", "ru_sub": "Подзаголовок."},
    {"id": "009", "src": "y.png", "head": "H", "sub": "S",
     "ru_head": "З", "ru_sub": "П"},
]
'''
    findings = _lint(_cards_ws(tmp_path, kit=kit))
    assert any(
        f.severity == "error" and "ST-009" in f.where
        and "not in the master" in f.message
        for f in findings
    ), [f.render() for f in findings]


def test_missing_story_kit_module_is_an_error(tmp_path: Path):
    ws = _ws(tmp_path, story_kit=None)  # workspace without story_kit.py
    findings = _lint(ws)
    assert any(
        f.severity == "error" and "cannot import story_kit.py" in f.message
        for f in findings
    ), [f.render() for f in findings]


# ── CLI ───────────────────────────────────────────────────────────────────────
def test_cli_lint_error_exits_1(tmp_path: Path):
    master = BASE_MASTER.replace("A clean line", "Powered by GitHub")
    ws = _ws(tmp_path, master=master)
    result = runner.invoke(
        standalone_app, ["channel", "lint", "--workspace", str(ws)]
    )
    assert result.exit_code == 1, result.output


def test_cli_lint_json(tmp_path: Path):
    ws = _ws(tmp_path)
    result = runner.invoke(
        standalone_app, ["channel", "lint", "--workspace", str(ws), "--json"]
    )
    assert result.exit_code == 0, result.output
    import json

    assert isinstance(json.loads(result.output), list)
