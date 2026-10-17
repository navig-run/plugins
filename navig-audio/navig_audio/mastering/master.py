"""The mastering pipeline: vocal + beat → a finished song at a measured loudness.

1. **pre-master** — the mix graph (chain.py) renders vocal + beat to a 32-bit float WAV.
2. **loudness** — gain toward the target, then a limiter at the true-peak ceiling, then
   *measure*; repeat with the measured error (≤ 4 rounds). A single loudnorm pass cannot do
   this for a loud target: to reach -9 LUFS it must limit, and loudnorm's linear mode
   refuses to, while its dynamic mode pumps. Gain + limiter + measurement is what a
   mastering engineer does by hand.
3. **encode** — mp3 320k (and a 24-bit WAV on request), measured again, and a sidecar JSON
   that holds the graph and every number, so a master can be reproduced or explained.

``reference`` hands step 2 to matchering (optional extra ``master``): the loudness, tone
and stereo width are matched to a reference track instead of a LUFS number.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from navig_sdk.host import decode_console_result

from navig_audio.mastering.chain import Preset, build_mix_graph, vocal_chain
from navig_audio.mastering.loudness import Loudness, measure

MAX_ROUNDS = 4
TOLERANCE_LU = 0.4
# alimiter is a sample-peak limiter; an mp3 encode adds inter-sample peaks on top. Keeping
# the ceiling this far under the true-peak target is what makes the *encoded* file honour it.
TP_MARGIN_DB = 0.8


class MasterError(RuntimeError):
    """ffmpeg is missing, refused the graph, or the result could not be measured."""


@dataclass
class MasterResult:
    out: Path
    wav: Path | None
    preset: str
    target_lufs: float
    lufs: float
    true_peak: float
    lra: float
    duration_s: float
    gain_db: float
    rounds: int
    vocal_in: dict
    beat_in: dict
    sidecar: Path | None = None

    def as_dict(self) -> dict:
        d = asdict(self)
        d.update(out=str(self.out), wav=str(self.wav) if self.wav else None,
                 sidecar=str(self.sidecar) if self.sidecar else None)
        return d


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise MasterError("ffmpeg is not on PATH — install it (scoop install ffmpeg) and try again")
    return exe


def _run(cmd: list[str], dst: Path, what: str, timeout: int) -> None:
    try:
        proc = decode_console_result(subprocess.run(cmd, capture_output=True, timeout=timeout, check=False))
    except subprocess.TimeoutExpired as exc:
        dst.unlink(missing_ok=True)
        raise MasterError(f"ffmpeg timed out on {what}") from exc
    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        dst.unlink(missing_ok=True)
        tail = (proc.stderr or "").strip().splitlines()
        raise MasterError(f"ffmpeg failed on {what}: {tail[-1] if tail else 'no output'}")


def _duration(path: Path) -> float:
    """Seconds, or 0.0 when ffprobe is missing or cannot read the file (callers treat 0 as unknown)."""
    from navig_media_core.media.audio_edit import AudioEditError, probe_duration  # noqa: PLC0415

    try:
        return probe_duration(path, timeout=60)
    except AudioEditError:
        return 0.0


def unique_path(path: Path) -> Path:
    """Never overwrite a master: ``name.master.mp3`` → ``name.master-take2.mp3`` …"""
    if not path.exists():
        return path
    n = 2
    while True:
        cand = path.with_name(f"{path.stem}-take{n}{path.suffix}")
        if not cand.exists():
            return cand
        n += 1


def processed_vocal_lufs(vocal: Path, preset: Preset, *, denoise: bool, vocal_gain_db: float, ready: bool = False,
                         work: Path, timeout: int, tone: str | None = None) -> float:
    """Render the vocal chain alone and measure it — the level the beat is set against."""
    dst = work / "vocal-only.wav"
    _run([_ffmpeg(), "-nostdin", "-y", "-i", str(vocal), "-af",
          vocal_chain(preset, denoise=denoise, gain_db=vocal_gain_db, ready=ready, tone=tone),
          "-c:a", "pcm_f32le", str(dst)],
         dst, "the vocal chain", timeout)
    lufs = measure(dst).lufs
    if lufs == float("-inf"):
        raise MasterError("the vocal is silent after processing — is it the right file?")
    return lufs


def premaster(vocal: Path, beat: Path, dst: Path, preset: Preset, *, offset_s: float = 0.0,
              vocal_gain_db: float = 0.0, duck_ratio: float | None = None, denoise: bool = True,
              ready: bool = False, tone: str | None = None,
              balance_lu: float | None = None, timeout: int = 900) -> tuple[str, float, float]:
    """Render the mix. Returns (graph, processed-vocal LUFS, the beat level it was set to)."""
    total = max(_duration(beat), offset_s + _duration(vocal)) or None
    v_lufs = processed_vocal_lufs(vocal, preset, denoise=denoise, vocal_gain_db=vocal_gain_db, ready=ready,
                                  work=dst.parent, timeout=timeout, tone=tone)
    beat_level = v_lufs - (preset.balance_lu if balance_lu is None else balance_lu)
    graph = build_mix_graph(preset, offset_s=offset_s, vocal_gain_db=vocal_gain_db,
                            duck_ratio=duck_ratio, denoise=denoise, total_s=total, beat_level=beat_level,
                            ready=ready, tone=tone)
    _run([_ffmpeg(), "-nostdin", "-y", "-i", str(vocal), "-i", str(beat), "-filter_complex", graph,
          "-map", "[mix]", "-c:a", "pcm_f32le", str(dst)], dst, "the pre-master mix", timeout)
    return graph, v_lufs, beat_level


def _limit(src: Path, dst: Path, gain_db: float, tp: float, timeout: int, margin_db: float = TP_MARGIN_DB) -> None:
    ceiling = 10 ** ((tp - margin_db) / 20)
    _run([_ffmpeg(), "-nostdin", "-y", "-i", str(src), "-af",
          # latency=1: without it alimiter's 5 ms lookahead delays the whole master and
          # cuts its last 5 ms (measured with navig audio enhance's alignment check).
          f"volume={gain_db:.3f}dB,alimiter=limit={ceiling:.5f}:attack=5:release=60:level=false:latency=1",
          "-c:a", "pcm_f32le", str(dst)], dst, "the limiter pass", timeout)


def to_loudness(src: Path, dst: Path, *, lufs: float, tp: float, timeout: int = 900,
                margin_db: float = TP_MARGIN_DB, start_gain: float | None = None) -> tuple[float, int, Loudness]:
    """Gain + limiter until the measured loudness is within TOLERANCE_LU of the target."""
    start = measure(src)
    if start.lufs == float("-inf"):
        raise MasterError("the mix is silent — nothing to master")
    gain = lufs - start.lufs if start_gain is None else start_gain
    got = start
    rounds = 0
    for rounds in range(1, MAX_ROUNDS + 1):
        _limit(src, dst, gain, tp, timeout, margin_db)
        got = measure(dst)
        error = lufs - got.lufs
        if abs(error) <= TOLERANCE_LU:
            break
        gain += error  # the limiter ate some of it: push by what is still missing
    return gain, rounds, got


def encode(src: Path, out: Path, *, wav: Path | None, timeout: int = 900) -> None:
    _run([_ffmpeg(), "-nostdin", "-y", "-i", str(src), "-c:a", "libmp3lame", "-b:a", "320k", str(out)],
         out, "the mp3 encode", timeout)
    if wav:
        _run([_ffmpeg(), "-nostdin", "-y", "-i", str(src), "-c:a", "pcm_s24le", str(wav)],
             wav, "the wav copy", timeout)


def match_reference(src: Path, reference: Path, dst: Path) -> None:
    """Reference mastering via matchering — imported lazily (optional extra ``master``)."""
    import matchering as mg  # noqa: PLC0415  (ImportError is the caller's to explain)

    mg.process(target=str(src), reference=str(reference), results=[mg.pcm24(str(dst))])


def master(vocal: Path, beat: Path, out: Path, preset: Preset, *, lufs: float | None = None,
           tp: float = -1.0, offset_s: float = 0.0, vocal_gain_db: float = 0.0,
           duck_ratio: float | None = None, denoise: bool = True, wav: bool = False, ready: bool = False,
           tone: str | None = None, balance_lu: float | None = None, reference: Path | None = None,
           timeout: int = 900) -> MasterResult:
    target = preset.lufs if lufs is None else lufs
    vocal_in, beat_in = measure(vocal), measure(beat)
    out.parent.mkdir(parents=True, exist_ok=True)
    wav_path = out.with_suffix(".wav") if wav else None
    with tempfile.TemporaryDirectory(prefix="navig-master-") as tmp:
        pre = Path(tmp) / "premaster.wav"
        graph, v_lufs, beat_level = premaster(
            vocal, beat, pre, preset, offset_s=offset_s, vocal_gain_db=vocal_gain_db,
            duck_ratio=duck_ratio, denoise=denoise, balance_lu=balance_lu, ready=ready, tone=tone,
            timeout=timeout)
        mastered = Path(tmp) / "master.wav"
        if reference:
            match_reference(pre, reference, mastered)
            gain, rounds = 0.0, 1
            encode(mastered, out, wav=wav_path, timeout=timeout)
            final = measure(out)
        else:
            # The encode adds inter-sample peaks the sample-peak limiter cannot see, and how
            # many depends on the material. So the ENCODED file is checked against the ceiling,
            # and the limiter is lowered by whatever it overshot (3 tries).
            margin = TP_MARGIN_DB
            gain = None
            rounds = 0
            for _ in range(3):
                gain, r, _ = to_loudness(pre, mastered, lufs=target, tp=tp, timeout=timeout,
                                         margin_db=margin, start_gain=gain)
                rounds += r
                encode(mastered, out, wav=wav_path, timeout=timeout)
                final = measure(out)
                if final.true_peak <= tp + 0.05:
                    break
                margin += final.true_peak - tp + 0.2
    final = measure(out)
    result = MasterResult(out=out, wav=wav_path, preset=preset.name, target_lufs=target, lufs=final.lufs,
                          true_peak=final.true_peak, lra=final.lra, duration_s=round(_duration(out), 2),
                          gain_db=round(gain, 2), rounds=rounds, vocal_in=vocal_in.as_dict(),
                          beat_in=beat_in.as_dict())
    sidecar = out.with_name(out.name + ".json")
    sidecar.write_text(json.dumps({
        **result.as_dict(), "vocal": str(vocal), "beat": str(beat), "reference": str(reference) if reference else None,
        "offset_s": offset_s, "vocal_gain_db": vocal_gain_db, "duck_ratio": duck_ratio, "denoise": denoise, "vocal_ready": ready,
        "voice_tone": tone or None,
        "true_peak_target": tp, "processed_vocal_lufs": round(v_lufs, 2), "beat_level_lufs": round(beat_level, 2),
        "balance_lu": preset.balance_lu if balance_lu is None else balance_lu, "graph": graph,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    result.sidecar = sidecar
    return result
