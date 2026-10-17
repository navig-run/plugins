"""navig-video — the render half of a channel: timeline → frames → ffmpeg.

A channel keeps ONE workspace of texts, plates, voice and beds (the owner's media
never moves); this plugin owns the code that turns it into video.

Wired today: ``status``/``doctor``, plus the kinetic engine —
``kinetic render`` (word-timed episodes at parity with the channel's own
renderer) and ``kinetic plan`` (the timeline without rendering). The rest arrive
as their engines are ported: narrated TRANSMISSION bulletins, the one-picture
formats (rollcall, montage) and per-destination encode variants. ``status``
computes each verb's wired/planned state from the live command tree; ``video_app``'s
own help is the authority on what exists — a docstring must not promise a verb
the CLI lacks.

Engines come from the plugins that own them: voice and beats from navig-audio, the
frame/mix primitives and plate loaders from navig-media-core (the shared engine core
re-exports after PR #1908), generator clients from navig-generate, ffmpeg probed on
PATH at runtime — never a pip dependency.
"""

from __future__ import annotations

__version__ = "0.3.0"
