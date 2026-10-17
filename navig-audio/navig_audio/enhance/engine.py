"""Audio Enhance: analyse → plan → render → verify → (retry safer) → export.

The original is never written: it is hashed before and after, every output gets a
fresh name (``<stem>.enhanced.<ext>``, then ``-take2`` …), and a run whose candidate
fails verification keeps nothing but the report. Internal processing stays in 32-bit
float WAV at the source's own rate and channel layout; the only lossy step is an
MP3 export, and only when asked for.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from navig_audio.enhance import planner as planner_mod
from navig_audio.enhance.analysis import AnalyzeError, AudioReport, analyze
from navig_audio.enhance.planner import SFX_PEAK_TARGET, Plan, Stage
from navig_audio.enhance.verify import Verification, verify

logger = logging.getLogger(__name__)

FORMATS = {
    "wav": (".wav", ["-c:a", "pcm_s24le"]),
    "wav16": (".wav", ["-c:a", "pcm_s16le"]),
    "flac": (".flac", ["-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24"]),
    "mp3": (".mp3", ["-c:a", "libmp3lame", "-b:a", "320k"]),
}
MAX_ATTEMPTS = 3
STEP_TIMEOUT_S = 900
LUFS_TOLERANCE = 0.5
# Two enhancement jobs at a time: each holds a decoded copy of its file in memory.
_SLOTS = threading.BoundedSemaphore(2)
_SAFE_STEM = re.compile(r"[^\w.\- ]+", re.UNICODE)


class EnhanceError(RuntimeError):
    """The file can't be enhanced (bad input, ffmpeg failure, the source changed)."""


class Cancelled(EnhanceError):
    """The job was cancelled; nothing was kept."""


@dataclass
class Attempt:
    stages: list[str]
    verdict: str
    summary: str


@dataclass
class EnhanceResult:
    source: str
    source_sha256: str
    mode: str
    report: AudioReport
    plan: Plan
    verdict: str  # clean (nothing to do) · pass · warn · fail · planned (dry run)
    outputs: list[str] = field(default_factory=list)
    verification: Verification | None = None
    attempts: list[Attempt] = field(default_factory=list)
    sidecar: str | None = None
    seconds: float = 0.0
    cost: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.verdict in ("clean", "pass", "warn", "planned")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source, "source_sha256": self.source_sha256, "mode": self.mode,
            "verdict": self.verdict, "ok": self.ok, "outputs": self.outputs,
            "report": self.report.to_dict(), "plan": self.plan.to_dict(),
            "verification": self.verification.to_dict() if self.verification else None,
            "attempts": [a.__dict__ for a in self.attempts], "sidecar": self.sidecar,
            "seconds": round(self.seconds, 2), "cost": self.cost,
        }


# ── helpers ──────────────────────────────────────────────────────────


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise EnhanceError("ffmpeg is not on PATH — install it (scoop install ffmpeg) and try again")
    return exe


def _run(args: list[str], dst: Path, what: str, cancel: threading.Event | None,
         *, argv: list[str] | None = None) -> None:
    """Run ffmpeg (or ``argv``), polling ``cancel``; a cancelled or failed step leaves no output."""
    proc = subprocess.Popen(  # noqa: S603 — argv list, no shell
        argv or [_ffmpeg(), "-hide_banner", "-nostdin", "-y", "-v", "error", *args],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + STEP_TIMEOUT_S
    while True:
        try:
            _, err = proc.communicate(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                proc.kill()
                proc.communicate()
                dst.unlink(missing_ok=True)
                raise Cancelled("cancelled") from None
            if time.monotonic() > deadline:
                proc.kill()
                proc.communicate()
                dst.unlink(missing_ok=True)
                raise EnhanceError(f"ffmpeg timed out on {what}") from None
    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        dst.unlink(missing_ok=True)
        tail = (err or b"").decode("utf-8", "replace").strip().splitlines()
        raise EnhanceError(f"ffmpeg failed on {what}: {tail[-1] if tail else 'no output'}")


def _measure(path: Path) -> tuple[float, float]:
    from navig_audio.mastering.loudness import measure

    m = measure(path)
    return m.lufs, m.true_peak


def unique_path(path: Path) -> Path:
    from navig_audio.mastering.master import unique_path as _unique

    return _unique(path)


def output_name(src: Path, out_dir: Path, ext: str, label: str = "enhanced") -> Path:
    """A fresh, *reserved* name: the file is created empty so a concurrent job can't take it."""
    stem = _SAFE_STEM.sub("_", src.stem).strip(" .") or "audio"
    for _ in range(1000):
        cand = unique_path(out_dir / f"{stem[:120]}.{label}{ext}")
        try:
            with cand.open("xb"):  # an empty placeholder: binary, no encoding involved
                return cand
        except FileExistsError:
            continue  # another job reserved it between the check and the create
    raise EnhanceError(f"could not find a free output name in {out_dir}")


DEEPFILTER_SHA256 = {
    # deep-filter-0.5.6-x86_64-pc-windows-msvc.exe from github.com/Rikorose/DeepFilterNet
    # (MIT/Apache-2.0). The release publishes no checksum; this one was taken on download
    # 2026-10-16 so a different binary in the same place is reported, not trusted silently.
    "75e11fa16445f560cb6b021521ddb89e89270d13b83089705d98776f58fd7915": "0.5.6",
}


def deepfilter_exe() -> Path | None:
    """The DeepFilterNet CLI, if installed: $NAVIG_DEEPFILTER → ~/.navig/bin → PATH."""
    import os

    cands: list[Path] = []
    env = os.environ.get("NAVIG_DEEPFILTER", "").strip()
    if env:
        cands.append(Path(env).expanduser())
    cands += [Path.home() / ".navig" / "bin" / "deep-filter.exe", Path.home() / ".navig" / "bin" / "deep-filter"]
    found = shutil.which("deep-filter")
    if found:
        cands.append(Path(found))
    return next((c for c in cands if c.is_file()), None)


def deepfilter_identity(exe: Path) -> str:
    """'0.5.6' for the known binary, 'unverified' for anything else at that path."""
    try:
        return DEEPFILTER_SHA256.get(sha256(exe), "unverified")
    except OSError:
        return "unreadable"


def available_engines() -> dict[str, bool]:
    """Which optional engines can run here. Never raises; never calls a network."""
    eleven = False
    try:
        from navig_media_core.tools.media_providers import resolve_media_key

        eleven = bool(resolve_media_key("elevenlabs", "ELEVENLABS_API_KEY", "ELEVEN_API_KEY"))
    except Exception:  # noqa: BLE001 — absent plugin or key store: the engine is just unavailable
        logger.debug("elevenlabs availability probe failed", exc_info=True)
    return {"ffmpeg": shutil.which("ffmpeg") is not None, "elevenlabs": eleven,
            "deepfilter": deepfilter_exe() is not None}


# ── rendering ────────────────────────────────────────────────────────


def _trimmed_seconds(report: AudioReport) -> float:
    start = max(0.0, report.lead_silence_s - 0.05)
    end = report.decoded_duration_s - max(0.0, report.trail_silence_s - 0.1)
    return end - start


def _chain(plan: Plan, report: AudioReport) -> str:
    parts: list[str] = []
    trim = next((s for s in plan.stages if s.name == "trim"), None)
    if trim is not None:
        start = max(0.0, report.lead_silence_s - 0.05)
        end = report.decoded_duration_s - max(0.0, report.trail_silence_s - 0.1)
        parts.append(f"atrim=start={start:.4f}:end={end:.4f},asetpts=PTS-STARTPTS")
    parts += [s.filter for s in plan.stages if s.filter]
    return ",".join(parts)


def chain_latency(src: Path, out: Path, report: AudioReport, *, max_ms: float = 250.0) -> int:
    """Samples by which ``out`` lags ``src`` (cross-correlation; 0 when aligned).

    Several ffmpeg denoisers delay their output without saying so: measured on a
    44.1 kHz file, ``afftdn`` by exactly 25 ms (1102 samples) and ``anlmdn`` by 8 ms,
    while keeping the length, so the tail is cut and every event lands late.
    """
    import numpy as np

    from navig_audio.enhance.analysis import decode

    n = int(min(report.decoded_duration_s, 12.0) * report.sample_rate)
    a = decode(src, report.sample_rate, report.channels).mean(axis=0)[:n].astype(np.float64)
    b = decode(out, report.sample_rate, report.channels).mean(axis=0)[:n].astype(np.float64)
    m = min(a.size, b.size)
    if m < 64:
        return 0
    size = 1 << int(np.ceil(np.log2(2 * m)))
    corr = np.fft.irfft(np.fft.rfft(b[:m], size) * np.conj(np.fft.rfft(a[:m], size)), size)
    w = int(report.sample_rate * max_ms / 1000)
    lags = np.concatenate((corr[: w + 1], corr[-w:]))
    idx = int(np.argmax(lags))
    return idx if idx <= w else idx - lags.size


def _deepfilter(src: Path, dst: Path, report: AudioReport, stage: Stage, work: Path,
                cancel: threading.Event | None) -> dict[str, Any]:
    """Run DeepFilterNet at its native 48 kHz, delay-compensated, then back to the source rate."""
    exe = deepfilter_exe()
    if exe is None:
        raise EnhanceError("DeepFilterNet (deep-filter.exe) is not installed")
    d48 = work / "dfn-in" / "speech.wav"
    d48.parent.mkdir(exist_ok=True)
    _run(["-i", str(src), "-vn", "-ar", "48000", "-ac", str(report.channels), "-c:a", "pcm_s16le", str(d48)],
         d48, "the 48 kHz copy for DeepFilterNet", cancel)
    out_dir = work / "dfn-out"
    out_dir.mkdir(exist_ok=True)
    produced = out_dir / d48.name
    limit = str(stage.settings.get("atten_lim_db", 30))
    _run([], produced, "DeepFilterNet", cancel,
         argv=[str(exe), "--compensate-delay", "-a", limit, "-o", str(out_dir), str(d48)])
    # Back to the source rate at EXACTLY the source length: the CLI's delay compensation
    # trims ~30 ms off the end (measured), which the duration check would rightly reject.
    n = int(round(report.decoded_duration_s * report.sample_rate))
    _run(["-i", str(produced), "-af", f"aresample={report.sample_rate},apad=whole_len={n},atrim=end_sample={n}",
          "-ac", str(report.channels), "-c:a", "pcm_f32le", str(dst)], dst, "the DeepFilterNet result", cancel)
    return {"engine": "deepfilter", "binary": deepfilter_identity(exe), "atten_lim_db": int(limit)}


def _isolate(src: Path, dst: Path) -> None:
    import asyncio

    from navig_generate.tools.audio_generation import AudioGenerationConfig, AudioGenerator

    async def _go() -> bytes:
        gen = AudioGenerator(AudioGenerationConfig(save_locally=False))
        try:
            return await gen.isolate_voice(src)
        finally:
            await gen.close()

    data = asyncio.run(_go())
    if not data:
        raise EnhanceError("ElevenLabs returned no audio for the isolation step")
    dst.write_bytes(data)


def _level(plan: Plan, src: Path, dst: Path, margin_db: float, cancel: threading.Event | None) -> dict[str, Any]:
    """Apply the plan's level stage (gain + true-peak limiter), measured, ≤ 4 rounds."""
    names = {s.name for s in plan.stages}
    lufs, tp_in = _measure(src)
    tp = plan.true_peak_db
    if plan.level_by == "lufs" and plan.target_lufs is not None and math.isfinite(lufs):
        gain = plan.target_lufs - lufs
    elif plan.level_by == "peak" and ("loudness" in names or "gain" in names):
        gain = (SFX_PEAK_TARGET if plan.mode != "preserve" else tp) - tp_in
    else:
        gain = 0.0  # limiter only: protect peaks, keep the recorded level
    ceiling = 10 ** ((tp - margin_db) / 20)
    rounds = 0
    got_lufs, got_tp = lufs, tp_in
    for rounds in range(1, 5):
        _run(["-i", str(src), "-af",
              # latency=1: alimiter's 5 ms lookahead otherwise shifts every event late.
              f"volume={gain:.3f}dB,alimiter=limit={ceiling:.5f}:attack=5:release=60:level=false:latency=1",
              "-c:a", "pcm_f32le", str(dst)], dst, "the level pass", cancel)
        got_lufs, got_tp = _measure(dst)
        if plan.level_by != "lufs" or plan.target_lufs is None or not math.isfinite(got_lufs):
            break
        err = plan.target_lufs - got_lufs
        if abs(err) <= LUFS_TOLERANCE:
            break
        gain += err
    return {"gain_db": round(gain, 2), "rounds": rounds, "lufs": got_lufs, "true_peak_db": got_tp}


def render(report: AudioReport, plan: Plan, work: Path, *, margin_db: float,
           cancel: threading.Event | None = None) -> tuple[Path, dict[str, Any]]:
    src = Path(report.path)
    current = src
    info: dict[str, Any] = {"graph": ""}
    if any(s.name == "isolate" for s in plan.stages):
        iso = work / "isolated.audio"
        _isolate(src, iso)
        current = iso
    dfn = next((s for s in plan.stages if s.engine == "deepfilter"), None)
    if dfn is not None:
        cleaned = work / "deepfiltered.wav"
        info["deepfilter"] = _deepfilter(current, cleaned, report, dfn, work, cancel)
        current = cleaned
    chain = _chain(plan, report)
    stage = work / "stage.wav"
    fmt = ["-ar", str(report.sample_rate), "-ac", str(report.channels), "-c:a", "pcm_f32le"]
    _run(["-i", str(current), "-vn", *(["-af", chain] if chain else []), *fmt, str(stage)],
         stage, "the processing chain", cancel)
    lag = chain_latency(current, stage, report) if chain and current in (src, work / "deepfiltered.wav") else 0
    if lag > 0:
        # Pad the end, run the chain, then cut exactly the delayed samples off the front:
        # same length, sample-aligned with the original.
        n = int(round(report.decoded_duration_s * report.sample_rate))
        if any(s.name == "trim" for s in plan.stages):
            n = int(round(_trimmed_seconds(report) * report.sample_rate))
        chain = (f"apad=pad_len={lag + 64},{chain},"
                 f"atrim=start_sample={lag}:end_sample={lag + n},asetpts=PTS-STARTPTS")
        _run(["-i", str(current), "-vn", "-af", chain, *fmt, str(stage)],
             stage, "the latency-compensated chain", cancel)
    info["graph"] = chain
    info["latency_samples"] = lag
    level_names = {"limit", "gain", "loudness"}
    if any(s.name in level_names for s in plan.stages):
        final = work / "final.wav"
        info["level"] = _level(plan, stage, final, margin_db, cancel)
        return final, info
    return stage, info


def export(candidate: Path, src: Path, out_dir: Path, formats: list[str],
           cancel: threading.Event | None = None, *, label: str = "enhanced") -> list[Path]:
    out: list[Path] = []
    for fmt in formats:
        ext, codec = FORMATS[fmt]
        dst = output_name(src, out_dir, ext, label)
        if dst.resolve() == src.resolve():  # unreachable with fresh names; kept as a hard stop
            raise EnhanceError("refusing to write over the original")
        try:
            _run(["-i", str(candidate), *codec, str(dst)], dst, f"the {fmt} export", cancel)
        except EnhanceError:
            for done in out:
                done.unlink(missing_ok=True)
            raise
        out.append(dst)
    return out


# ── the job ──────────────────────────────────────────────────────────


def enhance(
    src: Path | str,
    *,
    mode: str = "preserve",
    params: dict[str, Any] | None = None,
    out_dir: Path | str | None = None,
    formats: list[str] | None = None,
    category: str = "auto",
    dry_run: bool = False,
    force: bool = False,
    preset_id: str | None = None,
    cancel: threading.Event | None = None,
    progress: Callable[[str, float], None] | None = None,
) -> EnhanceResult:
    """Enhance one file. ``force`` processes even when the plan says nothing is needed."""
    t0 = time.monotonic()
    path = Path(src).expanduser()
    formats = formats or ["wav"]
    bad = [f for f in formats if f not in FORMATS]
    if bad:
        raise EnhanceError(f"unknown export format(s): {', '.join(bad)} (use {', '.join(FORMATS)})")
    params = dict(params or {})
    mode = str(params.pop("mode", mode))
    if "category" in params and category == "auto":
        category = str(params.pop("category"))
    say = progress or (lambda _msg, _frac: None)

    if not _SLOTS.acquire(timeout=600):
        raise EnhanceError("two enhancements are already running; try again in a moment")
    try:
        say("analyzing", 0.05)
        try:
            report = analyze(path, category=category)
        except AnalyzeError as exc:
            raise EnhanceError(str(exc)) from exc
        if cancel is not None and cancel.is_set():
            raise Cancelled("cancelled")
        if report.rms_db < -100 or not math.isfinite(report.peak_db) or report.peak_db < -100:
            raise EnhanceError(f"{path.name} is silent — there is nothing to enhance")
        digest = sha256(path)
        plan = planner_mod.plan(report, mode, params, engines=available_engines())
        if force and plan.empty:
            plan.stages.append(Stage("limit", "", f"Keep peaks under {plan.true_peak_db:.1f} dBTP"))
            plan.level_by = "peak"
        result = EnhanceResult(source=str(path), source_sha256=digest, mode=mode, report=report, plan=plan,
                               verdict="planned" if dry_run else "clean")
        if preset_id:
            result.cost["preset"] = preset_id
        if plan.cloud:
            result.cost["provider"] = "elevenlabs"
            result.cost["estimate_usd"] = round(max(report.decoded_duration_s, 1.0) / 60 * 0.12, 4)
        if dry_run or plan.empty:
            result.seconds = time.monotonic() - t0
            return result

        dest = Path(out_dir).expanduser() if out_dir else path.parent
        dest.mkdir(parents=True, exist_ok=True)
        margin = 0.8 if "mp3" in formats else 0.3
        current = plan
        with tempfile.TemporaryDirectory(prefix="navig-enhance-") as tmp:
            work = Path(tmp)
            for attempt in range(1, MAX_ATTEMPTS + 1):
                if cancel is not None and cancel.is_set():
                    raise Cancelled("cancelled")
                say(f"processing (attempt {attempt})", 0.15 + 0.25 * (attempt - 1))
                candidate, info = render(report, current, work, margin_db=margin, cancel=cancel)
                say("verifying", 0.45 + 0.2 * (attempt - 1))
                ver = verify(report, candidate, mode=mode, stages=[s.name for s in current.stages],
                             true_peak_db=current.true_peak_db, trim_lead_s=max(0.0, report.lead_silence_s - 0.05),
                             creative=current.creative)
                result.attempts.append(Attempt([s.name for s in current.stages], ver.verdict, ver.summary))
                result.verification = ver
                result.plan = current
                if ver.verdict != "fail":
                    say("exporting", 0.9)
                    outs = export(candidate, path, dest, formats, cancel)
                    result.outputs = [str(o) for o in outs]
                    result.verdict = ver.verdict
                    result.cost["render"] = info
                    break
                drop = current.riskiest()
                if drop is None or len(current.stages) <= 1:
                    break
                current = current.without(drop)
                if current.empty:
                    break
            else:  # pragma: no cover — loop always breaks or exhausts
                pass
            if not result.outputs:
                result.verdict = "fail"
        if sha256(path) != digest:
            raise EnhanceError("the original file changed while it was being enhanced — outputs not trusted")
        result.seconds = time.monotonic() - t0
        result.sidecar = _write_sidecar(result, dest, path)
        say("done", 1.0)
        return result
    finally:
        _SLOTS.release()


def _write_sidecar(result: EnhanceResult, dest: Path, src: Path) -> str:
    """Provenance next to the first output (or a report-only file when rejected)."""
    if result.outputs:
        side = Path(result.outputs[0] + ".enhance.json")
    else:
        side = unique_path(dest / f"{_SAFE_STEM.sub('_', src.stem)}.enhance-rejected.json")
    doc = result.to_dict()
    doc["tool"] = {"name": "navig audio enhance", "ffmpeg": shutil.which("ffmpeg") or ""}
    doc["note"] = ("Lossy source: exports are a cleaner rendering of what the encoder kept, not "
                   "recovered original detail.") if result.report.lossy else ""
    side.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(side)
