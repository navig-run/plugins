"""`navig audio gen` must not report a phantom "Generated N clip(s)".

The provider can return a result object with no/dead ``local_path`` (quota, a
rejected prompt, a bad voice id). The command used to print the green
"Generated N clip(s) · Saved under …" banner off ``len(results)`` regardless, so
the user was told audio was saved when nothing was written. It must count only
clips that actually wrote a file, and exit non-zero when none did.

No live ElevenLabs calls: the core ``navig.tools.audio_generation`` symbols are
monkeypatched (same seam as ``test_generation_facet``).
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from navig_audio.commands.audio import audio_app

runner = CliRunner()


class _Kind:
    def __init__(self, value: str) -> None:
        self.value = value


class _Aud:
    """Stand-in for GeneratedAudio as the command consumes it (kind/model/local_path)."""

    def __init__(self, local_path: str | None, kind: str = "music") -> None:
        self.local_path = local_path
        self.kind = _Kind(kind)
        self.model = "music_v1"

    def to_dict(self) -> dict:
        return {"local_path": self.local_path, "model": self.model}


def _patch(monkeypatch, gen_cls, out_dir: Path) -> None:
    import navig.tools.audio_generation as ag

    monkeypatch.setattr(ag, "is_audio_generation_available", lambda: True)

    class _Cfg:
        output_dir = str(out_dir)

        @classmethod
        def from_env(cls) -> "_Cfg":
            return cls()

    monkeypatch.setattr(ag, "AudioGenerationConfig", _Cfg)
    monkeypatch.setattr(ag, "AudioGenerator", gen_cls)


def test_gen_no_file_is_not_a_phantom_success(monkeypatch, tmp_path):
    class _DeadGen:
        def __init__(self, cfg) -> None:
            pass

        async def generate(self, *a, **kw) -> _Aud:
            return _Aud(None)  # provider returned a result but wrote nothing

        async def close(self) -> None:
            return None

    _patch(monkeypatch, _DeadGen, tmp_path)
    r = runner.invoke(audio_app, ["gen", "lofi", "--kind", "music"])
    assert r.exit_code == 1, r.output
    assert "No music clip was generated" in r.output
    assert "Generated 1" not in r.output  # the phantom-success banner must NOT appear


def test_gen_reports_the_real_count_on_success(monkeypatch, tmp_path):
    class _RealGen:
        def __init__(self, cfg) -> None:
            self.cfg = cfg

        async def generate(self, *a, **kw) -> _Aud:
            d = Path(self.cfg.output_dir)
            d.mkdir(parents=True, exist_ok=True)
            f = d / "clip.mp3"
            f.write_bytes(b"audio-bytes")
            return _Aud(str(f))

        async def close(self) -> None:
            return None

    _patch(monkeypatch, _RealGen, tmp_path)
    r = runner.invoke(audio_app, ["gen", "lofi", "--kind", "music"])
    assert r.exit_code == 0, r.output
    assert "Generated 1 music clip(s)" in r.output


def test_gen_json_exits_nonzero_when_all_failed(monkeypatch, tmp_path):
    class _DeadGen:
        def __init__(self, cfg) -> None:
            pass

        async def generate(self, *a, **kw) -> _Aud:
            return _Aud(None)

        async def close(self) -> None:
            return None

    _patch(monkeypatch, _DeadGen, tmp_path)
    r = runner.invoke(audio_app, ["gen", "lofi", "--json"])
    assert r.exit_code == 1  # --json must also signal failure, not exit 0
