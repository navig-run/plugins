"""Preflight: a doomed render is named before credits are spent.

`generate()` asks `_preflight` first — one free GET on the ElevenLabs meter — so an
exhausted plan or a creator-tier Flows call (402 `paid_plan_required`) is warned
about BEFORE the batch runs. Two properties are pinned: the probe never blocks a
render (any failure → no warnings), and it never fires for a provider it does not
serve (no key configured → no wasted roundtrip, no phantom warnings).
"""

from __future__ import annotations

import pytest

from navig_generate.media import generation_service as gs


def _sub_factory(payload):
    async def _sub(self):
        if isinstance(payload, Exception):
            raise payload
        return payload

    return _sub

@pytest.mark.asyncio
async def test_audio_default_probes_and_reports_exhaustion(monkeypatch):
    from navig_generate.tools.audio_generation import AudioGenerator

    monkeypatch.setattr(
        AudioGenerator, "subscription",
        _sub_factory({"tier": "creator", "character_count": 100, "character_limit": 100}),
    )
    warns = await gs._preflight("audio", None)
    assert any("exhausted" in w for w in warns), warns

@pytest.mark.asyncio
async def test_audio_healthy_plan_is_silent(monkeypatch):
    from navig_generate.tools.audio_generation import AudioGenerator

    monkeypatch.setattr(
        AudioGenerator, "subscription",
        _sub_factory({"tier": "creator", "character_count": 10, "character_limit": 1000}),
    )
    assert await gs._preflight("audio", None) == []

@pytest.mark.asyncio
async def test_near_limit_warns_before_the_batch_dies(monkeypatch):
    from navig_generate.tools.audio_generation import AudioGenerator

    monkeypatch.setattr(
        AudioGenerator, "subscription",
        _sub_factory({"tier": "creator", "character_count": 950, "character_limit": 1000}),
    )
    warns = await gs._preflight("audio", None)
    assert any("nearly exhausted" in w for w in warns), warns

@pytest.mark.asyncio
async def test_flows_video_on_a_creator_key_names_the_plan_gate(monkeypatch):
    from navig_generate.tools.audio_generation import AudioGenerator

    monkeypatch.setattr(
        AudioGenerator, "subscription",
        _sub_factory({"tier": "creator", "character_count": 1, "character_limit": 1000}),
    )
    warns = await gs._preflight("video", "elevenlabs")
    assert any("Pro plan or above" in w for w in warns), warns

@pytest.mark.asyncio
async def test_flows_video_on_a_pro_key_does_not_cry_wolf(monkeypatch):
    from navig_generate.tools.audio_generation import AudioGenerator

    monkeypatch.setattr(
        AudioGenerator, "subscription",
        _sub_factory({"tier": "pro", "character_count": 1, "character_limit": 1000}),
    )
    assert await gs._preflight("video", "elevenlabs") == []

@pytest.mark.asyncio
async def test_a_failing_probe_never_blocks_the_render(monkeypatch):
    from navig_generate.tools.audio_generation import AudioGenerator

    monkeypatch.setattr(
        AudioGenerator, "subscription", _sub_factory(RuntimeError("network down"))
    )
    assert await gs._preflight("audio", None) == []

@pytest.mark.asyncio
async def test_a_non_eleven_default_provider_never_probes(monkeypatch):
    """image defaults elsewhere (openai) — a subscription GET there would be waste."""
    from navig_generate.tools.audio_generation import AudioGenerator

    def _boom(self):
        raise AssertionError("preflight must not probe a non-ElevenLabs default")

    monkeypatch.setattr(AudioGenerator, "subscription", _boom)
    assert await gs._preflight("image", None) == []
    assert await gs._preflight("video", "gemini_veo") == []
