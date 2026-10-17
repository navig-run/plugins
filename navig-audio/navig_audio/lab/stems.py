"""Stem studio: split a song once (ElevenLabs), then remix it locally as often as you like.

``split``  uploads the song to ElevenLabs stem separation (two stems: vocals +
           instrumental, or six: drums, bass, guitar, piano, vocals, other) and keeps the
           parts in ``<name>.stems/`` — billed once, needs ``allow_cloud``.
``mix``    is local and free: mute, solo or re-balance any part (dB), or use a preset —
           karaoke (no vocals), acapella (vocals only), drumless, bassless — and render into
           ``<name>.stems/mixes/``.
"""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MIX_PRESETS: dict[str, dict[str, Any]] = {
    "karaoke": {"mute": ["vocals"]},
    "instrumental": {"mute": ["vocals"]},
    "acapella": {"solo": ["vocals"]},
    "drumless": {"mute": ["drums"]},
    "bassless": {"mute": ["bass"]},
}
AUDIO_EXTS = (".wav", ".mp3", ".flac")


class StemsError(RuntimeError):
    """No consent, provider failure, unknown stem, or ffmpeg failure."""


@dataclass
class SplitResult:
    folder: str
    stems: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def split(src: Path | str, *, six: bool = True, allow_cloud: bool = False, out_dir: Path | str | None = None,
          provider=None) -> SplitResult:
    if not allow_cloud:
        raise StemsError("splitting uploads the song to ElevenLabs (billed) — allow cloud processing for this run")
    from navig_audio.mastering.master import unique_path
    from navig_audio.regenerate.core import ElevenLabsProvider

    path = Path(src)
    if not path.is_file():
        raise StemsError(f"no such file: {path}")
    prov = provider or ElevenLabsProvider()
    blob = asyncio.run(prov.stems(path, six=six))
    base = Path(out_dir) if out_dir else path.parent
    folder = unique_path(base / f"{path.stem}.stems")
    folder.mkdir(parents=True)
    stems: dict[str, str] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            for name in z.namelist():
                leaf = Path(name).name
                if leaf.startswith(".") or Path(leaf).suffix.lower() not in AUDIO_EXTS:
                    continue
                dst = folder / leaf
                dst.write_bytes(z.read(name))
                stems[Path(leaf).stem.lower()] = str(dst)
    except zipfile.BadZipFile as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise StemsError("stem separation did not return a zip of stems") from exc
    if not stems:
        shutil.rmtree(folder, ignore_errors=True)
        raise StemsError("stem separation returned no audio parts")
    # File names only: the folder stays valid when it is moved or shared.
    (folder / "stems.json").write_text(json.dumps({"source": path.name, "six": six,
                                                   "stems": {k: Path(v).name for k, v in stems.items()}}, indent=2),
                                       encoding="utf-8")
    return SplitResult(str(folder), stems)


def list_stems(folder: Path | str) -> dict[str, Path]:
    """The split's parts — from ``stems.json`` when present, so rendered mixes never count as parts."""
    d = Path(folder)
    if not d.is_dir():
        raise StemsError(f"no such stems folder: {d}")
    manifest = d / "stems.json"
    found: dict[str, Path] = {}
    if manifest.is_file():
        try:
            listed = json.loads(manifest.read_text(encoding="utf-8")).get("stems", {})
            found = {k: d / Path(v).name for k, v in listed.items() if (d / Path(v).name).is_file()}
        except (OSError, ValueError, AttributeError):
            found = {}
    if not found:
        found = {f.stem.lower(): f for f in sorted(d.iterdir()) if f.is_file() and f.suffix.lower() in AUDIO_EXTS}
    if not found:
        raise StemsError(f"no stems in {d}")
    return found


def mix(folder: Path | str, *, preset: str | None = None, mute: list[str] | None = None, solo: list[str] | None = None,
        gains: dict[str, float] | None = None, out: Path | str | None = None, fmt: str = "wav") -> Path:
    """Render a local remix of the stems. Names match a stem by substring ('voc' → vocals)."""
    stems = list_stems(folder)
    spec = dict(MIX_PRESETS.get(preset or "", {}))
    if preset and preset not in MIX_PRESETS:
        raise StemsError(f"preset must be one of {', '.join(MIX_PRESETS)}")
    mute = [*spec.get("mute", []), *(mute or [])]
    solo = [*spec.get("solo", []), *(solo or [])]
    gains = dict(gains or {})

    def resolve(name: str) -> str:
        hits = [k for k in stems if name.lower() in k]
        if not hits:
            raise StemsError(f"no stem named like {name!r} (have: {', '.join(stems)})")
        return hits[0]

    muted = {resolve(m) for m in mute}
    soloed = {resolve(s) for s in solo}
    gain = {resolve(k): float(v) for k, v in gains.items()}
    chosen = [k for k in stems if (k in soloed if soloed else k not in muted)]
    if not chosen:
        raise StemsError("that leaves nothing to play")
    if "instrumental" in stems and "vocals" in stems and len(stems) == 2 and preset in ("karaoke", "instrumental"):
        chosen = ["instrumental"]
    inputs: list[str] = []
    for k in chosen:
        inputs += ["-i", str(stems[k])]
    graph = "".join(f"[{i}:a]volume={gain.get(k, 0.0):.2f}dB[s{i}];" for i, k in enumerate(chosen))
    graph += "".join(f"[s{i}]" for i in range(len(chosen))) + f"amix=inputs={len(chosen)}:normalize=0:duration=longest[mix]"
    label = preset or ("-".join(f"no-{m}" for m in sorted(muted)) or "remix")
    from navig_audio.enhance.engine import FORMATS
    from navig_audio.mastering.master import unique_path

    if fmt not in FORMATS:
        raise StemsError(f"format must be one of {', '.join(FORMATS)}")
    ext, codec = FORMATS[fmt]
    if out:
        dst = Path(out)
    else:
        (Path(folder) / "mixes").mkdir(exist_ok=True)
        dst = unique_path(Path(folder) / "mixes" / f"{label}{ext}")
    exe = shutil.which("ffmpeg")
    if not exe:
        raise StemsError("ffmpeg is not on PATH")
    proc = subprocess.run([exe, "-hide_banner", "-nostdin", "-y", "-v", "error", *inputs, "-filter_complex", graph,  # noqa: S603
                           "-map", "[mix]", *codec, str(dst)], capture_output=True, timeout=1800, check=False)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise StemsError(f"ffmpeg failed on the mix: {tail[-1] if tail else 'no output'}")
    return dst
