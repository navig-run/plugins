"""Universal presets reach the generator — and only where the caller left a gap.

`generate(presets=[...])` resolves through navig's preset catalog: a style clause is
appended after the user's own prompt (never replacing it), text presets arrive as the
backend's ``preset_system``, explicit request fields beat preset settings, and which
presets applied is stored in the variant's context.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytest.importorskip("navig.presets", reason="presets live in navig core")

from navig.presets import catalog  # noqa: E402
from navig.store import prompt_library  # noqa: E402
from navig_generate.media import generation_service as gs  # noqa: E402


@pytest.fixture
def presets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NAVIG_DATA_DIR", str(tmp_path / "data"))
    prompt_library.reset_prompt_store()
    d = tmp_path / "presets"
    d.mkdir()
    for data in (
        {"id": "image/style/noir", "name": "Noir", "tool": "image",
         "params": {"mood": "bleak", "aspect": "16:9", "negative": "text"}},
        {"id": "text/style/lit", "name": "Literary", "tool": "text", "instructions": "Write with restraint.",
         "params": {"kind": "article"}},
        {"id": "audio-generate/ui", "name": "UI", "tool": "audio.generate", "instructions": "Short blip.",
         "params": {"kind": "sfx", "duration_s": 0.6}},
    ):
        (d / (data["id"].replace("/", "_") + ".preset.yaml")).write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setattr(catalog, "preset_roots", lambda: [("builtin", d)])
    monkeypatch.setattr(catalog, "context_root", lambda: None)
    yield
    prompt_library.reset_prompt_store()


def test_image_style_is_appended_and_translated(presets):
    out = gs.apply_presets(modality="image", prompt="A lighthouse", presets=["image/style/noir"],
                           provider="openai_gpt_image")
    assert out["prompt"].startswith("A lighthouse\n\nStyle: ")
    assert "Avoid: text." in out["prompt"]
    assert out["size"] == "1536x1024"
    assert out["provenance"]["presets"][0]["id"] == "image/style/noir"


def test_explicit_request_beats_the_preset(presets):
    out = gs.apply_presets(modality="image", prompt="p", presets=["image/style/noir"], size="1024x1024")
    assert out["size"] == "1024x1024"
    out = gs.apply_presets(modality="audio", prompt="p", presets=["audio-generate/ui"], kind="music", duration_s=5)
    assert (out["kind"], out["duration_s"]) == ("music", 5)


def test_text_preset_becomes_the_system_addition(presets):
    out = gs.apply_presets(modality="text", prompt="Write the scene.", presets=["text/style/lit"])
    assert out["prompt"] == "Write the scene."
    assert "Write with restraint." in out["preset_system"]
    assert out["kind"] == "article"


def test_bad_preset_is_a_value_error_or_key_error(presets):
    with pytest.raises(KeyError):
        gs.apply_presets(modality="image", prompt="p", presets=["nope"])
    with pytest.raises(ValueError):
        gs.apply_presets(modality="text", prompt="p", presets=["image/style/noir"])


@pytest.mark.asyncio
async def test_generate_passes_preset_system_and_records_provenance(presets, monkeypatch, tmp_path):
    seen: dict = {}

    async def fake_group(**kw):
        seen.update(kw)
        return []

    monkeypatch.setattr(gs, "_generate_into_group", fake_group)
    monkeypatch.setattr(gs.refs_library, "resolve_refs_root", lambda _s: str(tmp_path / "refs" / "root"))
    monkeypatch.setattr(gs.refs_library, "ensure_layout", lambda _r: None)
    monkeypatch.setattr(
        gs.context_builder, "build_context",
        lambda prompt, **_kw: {"enriched_prompt": prompt, "context": {}},
    )
    result = await gs.generate(modality="text", prompt="Write the scene.", presets=["text/style/lit"])
    assert "Write with restraint." in seen["preset_system"]
    assert seen["kind"] == "article"
    assert result["context"]["presets"]["presets"][0]["id"] == "text/style/lit"


@pytest.mark.asyncio
async def test_generate_without_presets_is_unchanged(monkeypatch, tmp_path):
    seen: dict = {}

    async def fake_group(**kw):
        seen.update(kw)
        return []

    monkeypatch.setattr(gs, "_generate_into_group", fake_group)
    monkeypatch.setattr(gs.refs_library, "resolve_refs_root", lambda _s: str(tmp_path / "r" / "root"))
    monkeypatch.setattr(gs.refs_library, "ensure_layout", lambda _r: None)
    monkeypatch.setattr(
        gs.context_builder, "build_context",
        lambda prompt, **_kw: {"enriched_prompt": prompt, "context": {}},
    )
    await gs.generate(modality="audio", prompt="rain")
    assert seen["kind"] == "music" and seen["preset_system"] is None
    assert "presets" not in seen["context"]
