"""The voice remote: a fixed menu, nothing else reachable, going live never."""

from __future__ import annotations

import pytest

from navig_obs.show import remote

SCENES = [
    "01 Signal Acquisition",
    "02 The Architect",
    "05 Disorder Protocol",
    "12 Offline",
    "14 Emergency Cut",
]


class FakeStage:
    def __init__(self):
        self.moves: list[tuple] = []

    def scenes(self):
        return list(SCENES)

    def switch(self, scene):
        self.moves.append(("switch", scene))

    def record(self, start):
        self.moves.append(("record", start))

    def status(self):
        return {"scene": "02 The Architect", "streaming": False, "recording": True}

    def say(self, text, lang):
        self.moves.append(("say", text, lang))


@pytest.fixture
def rem():
    opened: list[str] = []
    r = remote.Remote(stage=FakeStage(), open_url=opened.append, games=lambda: ["A", "B", "C", "D"])
    r.opened = opened  # type: ignore[attr-defined]
    return r


@pytest.mark.parametrize(
    ("query", "scene"),
    [
        ("the architect", "02 The Architect"),
        ("architect", "02 The Architect"),
        ("two", "02 The Architect"),
        ("2", "02 The Architect"),
        ("scene five", "05 Disorder Protocol"),
        ("disorder protocol", "05 Disorder Protocol"),
        ("disorder protocole", "05 Disorder Protocol"),
        ("offline", "12 Offline"),
        ("douze", "12 Offline"),
        ("banana", None),
        ("", None),
    ],
)
def test_a_spoken_scene_finds_its_scene(query, scene):
    assert remote.match_scene(query, SCENES) == scene


def test_switch_scene_moves_the_program_and_answers_in_the_locale(rem):
    out = rem.handle({"intent": "SwitchScene", "slots": {"scene": "the architect"}, "locale": "fr-FR"})
    assert rem.stage.moves == [("switch", "02 The Architect")]
    assert out == {"speech": "À l'antenne : The Architect.", "done": "SwitchScene"}


def test_an_unknown_scene_changes_nothing(rem):
    out = rem.handle({"intent": "SwitchScene", "slots": {"scene": "banana"}})
    assert rem.stage.moves == [] and "banana" in out["speech"]


def test_emergency_cut_goes_to_the_safe_scene(rem):
    rem.handle({"intent": "EmergencyCut"})
    assert rem.stage.moves == [("switch", "14 Emergency Cut")]


def test_say_is_trimmed_and_spoken_in_the_locale_language(rem):
    rem.handle({"intent": "Say", "slots": {"text": "  bonjour   la Grille " + "x" * 400}, "locale": "fr-CA"})
    kind, text, lang = rem.stage.moves[0]
    assert kind == "say" and lang == "fr"
    assert text.startswith("bonjour la Grille") and len(text) == remote.MAX_SAY


def test_say_with_nothing_asks_back(rem):
    out = rem.handle({"intent": "Say", "slots": {}})
    assert rem.stage.moves == [] and out["speech"] == "What should the pigeon say?"


def test_status_reads_back_scene_live_and_recording(rem):
    out = rem.handle({"intent": "Status"})
    assert out["speech"] == "On air: The Architect. Not live. Recording."


def test_free_games_reads_at_most_three(rem):
    assert rem.handle({"intent": "FreeGames"})["speech"] == "Free today: A, B, C."


def test_only_allow_listed_https_pages_open(rem):
    rem.handle({"intent": "OpenPage", "slots": {"page": "Outpost"}})
    assert rem.opened == ["https://schema.cx/outpost"]
    out = rem.handle({"intent": "OpenPage", "slots": {"page": "file:///C:/Windows"}})
    assert rem.opened == ["https://schema.cx/outpost"] and "I can only open" in out["speech"]


@pytest.mark.parametrize("intent", ["StartStream", "StreamStart", "GoLive", "Run", "", None])
def test_going_live_and_anything_off_the_menu_is_unreachable(rem, intent):
    out = rem.handle({"intent": intent, "slots": {"command": "navig obs stream start"}})
    assert rem.stage.moves == [] and out["done"] is None


def test_the_menu_has_no_stream_start():
    assert not any("stream" in i.lower() or "live" in i.lower() for i in remote.INTENTS)
