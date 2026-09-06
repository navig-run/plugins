"""The content assembly line — compose capabilities across plugins.

The "OS system folders" payoff: one command chains standalone-yet-wired plugins
into a pipeline —

    acquire (navig-download) → transcribe (navig-audio) → script (navig-text)
        → narrate (navig-audio) → publish (navig-social)

Each stage is **soft-detected**: a plugin that isn't installed simply drops its
stage (logged, never a hard failure) — exactly the promise of independently
installable modules. This orchestrator is the one sanctioned place that reaches
across plugins; it does so through the same public seams a user would
(``navig.voice.transcribe`` shim, the generation facets, ``navig_social`` fan-out),
and it never lets a stage fail silently — every skip/failure is reported.
"""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# The ordered assembly line. Each stage maps to the plugin that provides it.
STAGE_PLUGIN = {
    "acquire": "navig-download",
    "transcribe": "navig-audio",
    "script": "navig-text",
    "narrate": "navig-audio",
    "publish": "navig-social",
}
_MODULE = {
    "acquire": "navig_download",
    "transcribe": "navig_audio",
    "script": "navig_text",
    "narrate": "navig_audio",
    "publish": "navig_social",
}


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):  # pragma: no cover - find_spec on a broken pkg
        return False


def detect_capabilities() -> dict[str, bool]:
    """Which stages can run right now (their providing plugin is installed)."""
    return {stage: _installed(mod) for stage, mod in _MODULE.items()}


@dataclass
class StageResult:
    """One stage's outcome — never silent: `skipped`/`failed` carry a reason."""

    name: str
    status: str  # ran | preview | skipped | failed
    detail: str = ""
    artifact: str | None = None


@dataclass
class PipelineReport:
    stages: list[StageResult] = field(default_factory=list)
    caption: str | None = None
    narration_path: str | None = None
    source_path: str | None = None
    receipts: list[Any] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "", artifact: str | None = None) -> StageResult:
        r = StageResult(name, status, detail, artifact)
        self.stages.append(r)
        return r

    def to_dict(self) -> dict[str, Any]:
        return {
            "stages": [vars(s) for s in self.stages],
            "caption": self.caption,
            "narration_path": self.narration_path,
            "source_path": self.source_path,
            "published": [getattr(r, "to_dict", lambda: r)() for r in self.receipts],
        }


# ── capability calls (soft imports; each isolated so one missing plugin can't
#    break the chain). Kept module-level so tests can monkeypatch them. ─────────


async def _do_script(topic: str, *, kind: str, out_dir: Path) -> str:
    """navig-text: draft the social caption/brief. Returns the text."""
    from navig_text.generation import generate_text_docs

    docs = await generate_text_docs(topic, kind=kind, n=1, out_dir=out_dir)
    return docs[0].text.strip()


async def _do_narrate(script: str, *, out_dir: Path) -> str | None:
    """navig-audio: turn the script into a voiceover (audio gen, TTS). Path or None."""
    from navig_audio.generation import _audio_backend

    clips = await _audio_backend(script, kind="tts", n=1, out_dir=out_dir)
    return clips[0].local_path if clips else None


async def _do_acquire(url: str, *, out_dir: Path) -> str:
    """navig-download: fetch a source URL to a local file (yt-dlp). Returns the path."""
    from navig_download.tiktok.engine import fetch_file_async

    return await fetch_file_async(url, dest_dir=str(out_dir))


async def _do_transcribe(media_path: str) -> str | None:
    """navig-audio (voice STT via the core shim): media → transcript, or None if
    nothing was recognized (a failure the caller must surface, not mask as empty)."""
    from navig.voice import transcribe

    return await transcribe(media_path)


async def _do_publish(caption: str, *, url: str | None, to: list[str], campaign: str | None, dry_run: bool):
    """navig-social: fan the caption out (or preview it). Returns receipts/preview rows."""
    from navig_social.social.fanout import fan_out, preview

    brief = {"title": caption.splitlines()[0][:120] if caption else "Update", "body": caption, "url": url or ""}
    if dry_run:
        return preview(brief, platforms=to, campaign=campaign)
    return await fan_out(brief, platforms=to, campaign=campaign)


async def run_pipeline(
    *,
    topic: str | None = None,
    source: str | None = None,
    to: list[str] | None = None,
    kind: str = "social",
    narrate: bool = False,
    campaign: str | None = None,
    url: str | None = None,
    dry_run: bool = True,
    work_dir: str | Path | None = None,
) -> PipelineReport:
    """Run the assembly line. ``topic`` (text-first) or ``source`` (a media file to
    transcribe first) seeds it; the drafted caption fans out to ``to``.

    ``dry_run`` (default) generates the real caption + shows the fan-out preview
    but performs no download / narration / publish side effects.
    """
    to = to or ["x", "telegram"]
    caps = detect_capabilities()
    out = Path(work_dir) if work_dir else Path("~/.navig/pipeline").expanduser()
    out.mkdir(parents=True, exist_ok=True)
    report = PipelineReport()

    # 1. acquire — fetch a source URL to a local file (navig-download); local files pass through.
    transcript = None
    if source:
        is_url = source.startswith(("http://", "https://"))
        if is_url and not caps["acquire"]:
            report.add("acquire", "skipped", "navig-download not installed")
        elif is_url and dry_run:
            report.add("acquire", "skipped", f"dry-run (would download {source} via navig-download)")
        elif is_url:
            try:
                report.source_path = await _do_acquire(source, out_dir=out)
                report.add("acquire", "ran", f"downloaded → {report.source_path}")
            except Exception as exc:  # noqa: BLE001 - yt-dlp missing / unsupported URL / network
                report.add("acquire", "failed", str(exc))
        else:  # a local file path
            report.source_path = source
            report.add("acquire", "ran", f"local source {source}")

        # 2. transcribe — media → transcript (navig-audio voice STT via the core shim).
        if not caps["transcribe"]:
            report.add("transcribe", "skipped", "navig-audio not installed")
        elif dry_run:
            report.add("transcribe", "skipped", "dry-run (would transcribe via navig-audio)")
        elif report.source_path:  # only when acquire actually produced a file
            try:
                result = await _do_transcribe(report.source_path)
                if result and result.strip():
                    transcript = result
                    report.add("transcribe", "ran", f"{len(transcript)} chars",
                               artifact=transcript[:80])
                else:
                    # None/empty = STT recognized nothing — a failure, not a 0-char success.
                    report.add("transcribe", "failed", "no speech recognized (empty transcript)")
            except Exception as exc:  # noqa: BLE001 - surface, don't crash the line
                report.add("transcribe", "failed", str(exc))

    # 3. script — draft the post; be explicit about which seed was used.
    if topic:
        seed_text, seed_note = topic, "topic"
    elif transcript:
        seed_text, seed_note = transcript, "transcript"
    elif source:
        seed_text, seed_note = f"Summarize and promote: {source}", "source URL (no transcript)"
    else:
        seed_text, seed_note = None, ""
    if not seed_text:
        report.add("script", "failed", "no topic or source to write from")
        return report
    if not caps["script"]:
        report.add("script", "skipped", "navig-text not installed — cannot draft the post")
        return report
    try:
        report.caption = await _do_script(seed_text, kind=kind, out_dir=out)
    except Exception as exc:  # noqa: BLE001
        report.add("script", "failed", str(exc))
        return report
    if not report.caption or not report.caption.strip():
        # Defensive: an empty draft must not become a live "Update" placeholder.
        report.caption = None
        report.add("script", "failed", "draft came back empty — not publishing a placeholder")
        return report
    report.add("script", "ran", f"{len(report.caption)} chars via navig-text (from {seed_note})",
               artifact=report.caption)

    # 4. narrate (optional) — audio gen wired in as the voiceover step
    if narrate:
        if not caps["narrate"]:
            report.add("narrate", "skipped", "navig-audio not installed")
        elif dry_run:
            report.add("narrate", "skipped", "dry-run (would voice the script via navig-audio audio gen)")
        else:
            try:
                report.narration_path = await _do_narrate(report.caption, out_dir=out)
                report.add("narrate", "ran" if report.narration_path else "failed",
                           report.narration_path or "no audio produced", artifact=report.narration_path)
            except Exception as exc:  # noqa: BLE001 - e.g. no ElevenLabs key
                report.add("narrate", "failed", str(exc))

    # 5. publish (or preview)
    if not caps["publish"]:
        report.add("publish", "skipped", "navig-social not installed")
        return report
    try:
        report.receipts = await _do_publish(
            report.caption, url=url, to=to, campaign=campaign, dry_run=dry_run
        )
        if dry_run:
            report.add("publish", "preview", f"{len(to)} network(s): {', '.join(to)}")
        else:
            # Status comes from the RECEIPTS, not the request — a failure receipt must
            # not read as "ran". (fan_out never raises; it returns failure receipts.)
            total = len(report.receipts)
            ok = sum(1 for r in report.receipts if getattr(r, "ok", False))
            if total and ok == total:
                report.add("publish", "ran", f"published to {ok}/{total}: {', '.join(to)}")
            else:
                fails = "; ".join(
                    f"{getattr(r, 'network', '?')}: {getattr(r, 'error', None) or 'failed'}"
                    for r in report.receipts if not getattr(r, "ok", False)
                ) or "no networks reached"
                report.add("publish", "failed", f"{ok}/{total} ok — {fails}")
    except Exception as exc:  # noqa: BLE001
        report.add("publish", "failed", str(exc))
    return report


# ── autopilot: compose a recurring `navig pipeline run --live` for the scheduler ──


def _shq(value: str) -> str:
    """Quote a value for the scheduler's ``shlex.split`` parser.

    ``shlex.quote`` handles every edge (spaces, quotes, trailing backslash) so the
    stored command round-trips through the cron service unchanged.
    """
    import shlex

    return shlex.quote(str(value))


def compose_run_command(
    *,
    topic: str | None = None,
    source: str | None = None,
    to: list[str],
    kind: str = "social",
    narrate: bool = False,
    campaign: str | None = None,
    url: str | None = None,
) -> str:
    """Build the ``navig pipeline run … --live`` command a cron job will fire.

    Deterministic + quote-safe so the composed string round-trips through the
    scheduler unchanged (the autopilot re-drafts fresh content every cadence).
    """
    parts = ["navig", "pipeline", "run"]
    if topic:
        parts += ["--topic", _shq(topic)]
    if source:
        parts += ["--source", _shq(source)]
    parts += ["--to", ",".join(to)]
    if kind and kind != "social":
        parts += ["--kind", kind]
    if narrate:
        parts.append("--narrate")
    if campaign:
        parts += ["--campaign", _shq(campaign)]
    if url:
        parts += ["--url", _shq(url)]
    parts.append("--live")
    return " ".join(parts)


def autopilot_job_name(
    *, topic: str | None = None, source: str | None = None, name: str | None = None
) -> str:
    """A stable, human-readable cron job name for a scheduled pipeline."""
    if name:
        return name
    seed = (topic or source or "content").lower()
    slug = "-".join("".join(c if c.isalnum() else " " for c in seed).split()[:4]) or "content"
    return f"autopilot-{slug}"
