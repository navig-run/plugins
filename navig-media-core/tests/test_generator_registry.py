"""One owner per modality: a second plugin cannot silently take over a generator."""

from __future__ import annotations

import logging

import pytest

from navig_media_core.media import types as T


@pytest.fixture(autouse=True)
def _clean_registry():
    saved = (dict(T._GENERATORS), dict(T._OWNERS))
    T._GENERATORS.clear()
    T._OWNERS.clear()
    yield
    T._GENERATORS.clear()
    T._OWNERS.clear()
    T._GENERATORS.update(saved[0])
    T._OWNERS.update(saved[1])


async def _a(*_a, **_k):
    return ["a"]


async def _b(*_a, **_k):
    return ["b"]


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_first_owner_wins_and_a_different_owner_is_refused() -> None:
    # A handler on the logger itself: navig's loggers may not propagate to caplog's root.
    log, handler = logging.getLogger("navig.media.types"), _Collect()
    log.addHandler(handler)
    try:
        assert T.register_generator("audio", _a, owner="navig_audio") is True
        assert T.register_generator("audio", _b, owner="navig_other") is False
    finally:
        log.removeHandler(handler)
    assert T.get_generator("audio") is _a
    assert T.registered_generators() == {"audio": "navig_audio"}
    assert any("navig_audio" in m and "navig_other" in m for m in handler.messages), handler.messages


def test_same_owner_reregistering_replaces_its_backend() -> None:
    T.register_generator("text", _a, owner="navig_text")
    assert T.register_generator("text", _b, owner="navig_text") is True
    assert T.get_generator("text") is _b


def test_result_does_not_depend_on_registration_order() -> None:
    T.register_generator("image", _a, owner="p1")
    T.register_generator("image", _b, owner="p2")
    first = T.get_generator("image")
    T.unregister_generator("image")
    T.register_generator("image", _a, owner="p1")
    T.register_generator("image", _b, owner="p2")
    assert T.get_generator("image") is first is _a


def test_explicit_replace_overrides_and_records_the_new_owner() -> None:
    T.register_generator("video", _a, owner="p1")
    assert T.register_generator("video", _b, owner="p2", replace=True) is True
    assert T.registered_generators() == {"video": "p2"}


def test_owner_defaults_to_the_backend_top_level_package() -> None:
    T.register_generator("audio", _a)
    assert T.registered_generators()["audio"] == __name__.split(".")[0]


def test_positional_two_argument_call_still_works() -> None:
    """Existing facets (audio, text) call register_generator(modality, backend)."""
    T.register_generator(T.MediaModality.TEXT, _a)
    assert T.get_generator(T.MediaModality.TEXT) is _a
    T.unregister_generator(T.MediaModality.TEXT)
    assert T.get_generator("text") is None
