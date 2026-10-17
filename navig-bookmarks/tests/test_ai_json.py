"""AI replies: strict parse, forgiving extraction, truncated-JSON repair that never invents."""

from __future__ import annotations

import json

import pytest

from navig_bookmarks.ai_json import AIResponseError, parse_ai_json, repair_truncated_json

GOOD = {"assignments": [{"id": "a", "folder": "Dev"}, {"id": "b", "folder": "Home/Recipes"}]}


@pytest.mark.parametrize(
    "reply",
    [
        json.dumps(GOOD),
        "```json\n" + json.dumps(GOOD) + "\n```",
        "Sure! Here is the plan:\n" + json.dumps(GOOD) + "\nHope that helps.",
        "<think>let me consider {braces} and [brackets]</think>\n" + json.dumps(GOOD),
    ],
)
def test_wrapped_replies_parse(reply: str) -> None:
    assert parse_ai_json(reply) == GOOD


def test_truncated_list_keeps_only_complete_elements() -> None:
    text = '{"assignments": [{"id": "a", "folder": "Dev"}, {"id": "b", "folder": "Home/Rec'
    repaired = json.loads(repair_truncated_json(text))
    # The half-written element is cut back to its last complete field — "b" has no folder, so
    # the planner treats it as missing and retries it. Nothing is invented.
    assert repaired["assignments"][0] == {"id": "a", "folder": "Dev"}
    assert all(row.get("folder") in (None, "Dev") for row in repaired["assignments"])


def test_truncated_inside_a_string_with_escapes() -> None:
    text = '{"assignments": [{"id": "a", "folder": "Quotes \\"and\\" commas, here"}, {"id": "b", "fol'
    repaired = json.loads(repair_truncated_json(text))
    assert repaired["assignments"][0]["folder"] == 'Quotes "and" commas, here'


def test_truncated_reply_parses_through_the_front_door() -> None:
    text = '```json\n{"tags": [{"id": "x", "tags": ["python", "async"]}, {"id": "y", "tags": ["re'
    obj = parse_ai_json(text)
    assert obj["tags"][0] == {"id": "x", "tags": ["python", "async"]}


def test_complete_document_with_trailing_junk() -> None:
    assert json.loads(repair_truncated_json('{"a": [1, 2]} trailing words')) == {"a": [1, 2]}


@pytest.mark.parametrize("reply", ["", "   ", "no json here at all", '{"assignments": [{"id": "a"'])
def test_hopeless_replies_raise(reply: str) -> None:
    with pytest.raises(AIResponseError):
        parse_ai_json(reply)
