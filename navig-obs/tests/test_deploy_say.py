"""A landed patch, spoken in the stage's language — and only while on air."""

from __future__ import annotations

import asyncio

import pytest

from navig_obs.show import feed, voices
from navig_obs.show.bridge import ShowBridge
from navig_obs.show.settings import ShowSettings


@pytest.mark.parametrize(
    ("lang", "line"),
    [
        ("fr", "SCHEMA est mis à jour. L’Architecte a touché les Salles et la Grille."),
        ("ru", "SCHEMA обновлена. Архитектор изменил: Комнаты, Сетка."),
        ("en", "SCHEMA updated. The Architect touched the Rooms and the Grid."),
    ],
)
def test_the_layers_are_named_in_the_stage_language(lang, line):
    assert voices.deploy_line(["rooms", "grid"], lang) == line


def test_english_reads_the_patch_headline_when_there_is_one():
    assert voices.deploy_line(["kernel"], "en", "Ensina's narration now finishes its sentences.") == (
        "SCHEMA updated. Ensina's narration now finishes its sentences."
    )


def test_a_long_patch_names_three_layers_and_counts_the_rest():
    line = voices.deploy_line(["rooms", "grid", "mask", "gate", "wire"], "fr")
    assert line.endswith("le Masque, et 2 autres.")


def test_an_internal_patch_says_nothing():
    assert voices.deploy_line([], "fr") is None
    assert voices.deploy_line(["not-a-layer"], "ru") is None


def test_every_layer_has_all_three_languages():
    for key, names in voices.LAYER_NAMES.items():
        assert set(names) == set(voices.LANGS), key


def test_the_feed_carries_the_layer_keys():
    prev = feed.FeedState(seq=1)
    doc = {"patches": [{"seq": 2, "headline": "h", "publicCount": 1,
                        "layers": [{"key": "rooms", "count": 2}, {"key": "grid", "count": 1}]}]}
    toasts, _ = feed.diff(prev, doc)
    assert toasts[0]["layers"] == ["rooms", "grid"]


class Stage:
    """A fake OBS: on air or not, in one language."""

    def __init__(self, live: bool, collection: str = "SCHEMA BROADCAST (fr)"):
        self.live, self.collection = live, collection
        self.calls: list[tuple[str, dict | None]] = []

    def connect(self):
        pass

    def close(self):
        pass

    def request(self, rtype, data=None):
        self.calls.append((rtype, data))
        if rtype in ("GetStreamStatus", "GetRecordStatus"):
            return {"outputActive": self.live and rtype == "GetStreamStatus"}
        if rtype == "GetSceneCollectionList":
            return {"currentSceneCollectionName": self.collection}
        return {}


def _bridge(tmp_path, stage, **kw):
    said: list = []

    def prep(p):
        said.append((p.text, p.lang, p.provider))
        p.ms, p.clip = 1000, None
        return p

    s = ShowSettings(enabled=True, patch_feed="", civic_feed="", cues_file=tmp_path / "none.json",
                     say_voices={"fr": {"provider": "edge", "voice": "fr-FR-HenriNeural"}}, **kw)
    return ShowBridge(s, client_factory=lambda: stage, state_path=tmp_path / "st.json",
                      voice_prepare=prep), said


PATCH = {"kind": "patch", "seq": 9, "version": "1.6.7", "headline": "h", "count": 2,
         "layers": ["rooms", "grid"]}


def _events(stage):
    return [d["requestData"]["event_name"] for t, d in stage.calls if t == "CallVendorRequest"]


def test_on_air_the_patch_is_spoken_in_the_stage_language(tmp_path):
    stage = Stage(live=True)
    b, said = _bridge(tmp_path, stage, deploy_say=True)
    assert asyncio.run(b.toast(dict(PATCH)))
    assert said == [("SCHEMA est mis à jour. L’Architecte a touché les Salles et la Grille.", "fr", "edge")]
    toast = next(d["requestData"]["event_data"] for t, d in stage.calls if t == "CallVendorRequest")
    assert toast["kind"] == "alert" and toast["tone"] == "deploy" and toast["kicker"] == "SCHEMA 1.6.7"


def test_off_air_the_patch_is_the_silent_card_as_before(tmp_path):
    stage = Stage(live=False)
    b, said = _bridge(tmp_path, stage, deploy_say=True)
    asyncio.run(b.toast(dict(PATCH)))
    assert said == []
    assert _events(stage) == ["schema-toast"]
    card = next(d["requestData"]["event_data"] for t, d in stage.calls if t == "CallVendorRequest")
    assert card["kind"] == "patch"


def test_deploy_say_off_keeps_the_old_behaviour_even_on_air(tmp_path):
    stage = Stage(live=True)
    b, said = _bridge(tmp_path, stage)
    asyncio.run(b.toast(dict(PATCH)))
    assert said == [] and not any(t == "GetStreamStatus" for t, _ in stage.calls)


def test_the_guard_can_be_turned_off(tmp_path):
    stage = Stage(live=False)
    b, said = _bridge(tmp_path, stage, deploy_say=True, say_on_air_only=False)
    asyncio.run(b.toast(dict(PATCH)))
    assert len(said) == 1


def test_civic_speech_waits_for_air_too(tmp_path):
    docs = iter([
        {"data": [{"occurredAt": "2026-10-01T00:00:00Z", "kind": "ignition", "zone": {"name": "Kazan"}}]},
        {"data": [{"occurredAt": "2026-10-02T00:00:00Z", "kind": "ignition", "zone": {"name": "Kazan"}}]},
    ])
    stage = Stage(live=False)
    b, said = _bridge(tmp_path, stage, civic_say=True)
    b._fetcher = lambda _u: next(docs)
    b.settings = ShowSettings(**{**b.settings.__dict__, "civic_feed": "https://civic.test"})
    asyncio.run(b.poll_civic_once())
    asyncio.run(b.poll_civic_once())
    assert said == []  # off air: the pigeon reacts, but says nothing
    assert _events(stage) == ["pigeonCivic"]
