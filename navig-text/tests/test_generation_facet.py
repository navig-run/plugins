"""TEXT generation facet — registry wiring + doc generation (no live LLM calls).

Core's ``get_ai_client`` is monkeypatched with a fake whose ``complete`` echoes
the prompt, so nothing hits a provider. Asserts the facet registers a TEXT
backend, honours ``n``, and maps the engine's default ``kind`` safely.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from navig_text.generation import _text_backend, generate_text_docs, register_text_facet


class _FakeClient:
    provider = "fake"
    model = "fake-model"

    def is_available(self) -> bool:
        return True

    async def complete(self, prompt, system_prompt=None, **_kw) -> str:
        return f"# Draft\n\n{prompt}\n\n(system set: {bool(system_prompt)})"


@pytest.fixture(autouse=True)
def _patch_client(monkeypatch):
    import navig.agent.ai_client as aic

    monkeypatch.setattr(aic, "get_ai_client", lambda: _FakeClient())


def test_generate_writes_n_distinct_docs(tmp_path):
    out = tmp_path / "docs"
    results = asyncio.run(generate_text_docs("vector databases", kind="draft", n=3, out_dir=out))

    assert len(results) == 3
    paths = {r.local_path for r in results}
    assert len(paths) == 3, "n>1 must produce distinct files"
    for r in results:
        assert Path(r.local_path).exists()
        assert "vector databases" in r.text


def test_backend_maps_engine_default_kind_to_draft(tmp_path):
    # The engine's default kind is the audio-centric "music"; the text facet must
    # not choke on it — it falls back to "draft".
    results = asyncio.run(_text_backend("hello world", kind="music", n=1, out_dir=tmp_path))
    assert len(results) == 1
    assert results[0].kind == "draft"
    assert Path(results[0].local_path).name == "text.md"  # n==1 keeps the plain name


def test_register_text_facet_wires_registry():
    from navig.media.types import MediaModality, get_generator

    register_text_facet()
    assert get_generator(MediaModality.TEXT) is _text_backend


def test_empty_completion_raises(monkeypatch, tmp_path):
    # A blank/whitespace completion is a provider failure — surface it, never write
    # an empty doc a caller would publish as a placeholder.
    import navig.agent.ai_client as aic

    class _Empty:
        model = "m"

        def is_available(self):
            return True

        async def complete(self, prompt, system_prompt=None, **_kw):
            return "   "

    monkeypatch.setattr(aic, "get_ai_client", lambda: _Empty())
    with pytest.raises(RuntimeError):
        asyncio.run(generate_text_docs("x", kind="draft", n=1, out_dir=tmp_path))
