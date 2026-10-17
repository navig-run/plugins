"""Podcast episode compilation — a Markdown scenario becomes an album of audio tracks.

The pipeline, and the one human checkpoint in the middle of it:

    rough notes ──draft──▶ scenario.md ──translate──▶ scenario.<lang>.md
                              │  (you review and edit this)
                              └────render────▶ tracks + master + subtitles + metadata

Each stage is its own command under ``navig audio`` so the expensive one (``render``) is
never reached without the cheap ones (``plan``, and your own eyes) having run first.

Modules:

* :mod:`~navig_audio.podcast.scenario`  — parse and emit the strict scenario format
* :mod:`~navig_audio.podcast.chunker`   — split tracks into requests without losing the take
* :mod:`~navig_audio.podcast.cost`      — what it will cost, before it costs it
* :mod:`~navig_audio.podcast.cache`     — content-addressed clips, so re-runs are free
* :mod:`~navig_audio.podcast.srt`       — subtitles from the timings of the shipped audio
* :mod:`~navig_audio.podcast.voices`    — speaker names to voice ids, and cloning
* :mod:`~navig_audio.podcast.draft`     — rough notes to strict scenario
* :mod:`~navig_audio.podcast.translate` — same album, different language
* :mod:`~navig_audio.podcast.render`    — the build
* :mod:`~navig_audio.podcast.publish`   — the NobiCast contract (not wired yet)
"""

from __future__ import annotations

from navig_audio.podcast.scenario import (
    Cue,
    Episode,
    Line,
    ScenarioError,
    Track,
    VoiceSpec,
    dump,
    load,
    parse,
    slugify,
)

__all__ = [
    "Cue",
    "Episode",
    "Line",
    "ScenarioError",
    "Track",
    "VoiceSpec",
    "dump",
    "load",
    "parse",
    "slugify",
]
