"""Compat alias — the loudness meter is navig-media-core's (``navig_media_core.media.loudness``).

One meter for every media package (ADR-0002). Aliased rather than re-exported, so
``LoudnessError``/``Loudness``/``measure`` are the SAME objects and dotted-path patches of
``navig_audio.mastering.loudness.<name>`` still reach the one real module.
"""

from __future__ import annotations

import sys as _sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - static analysis only
    from navig_media_core.media.loudness import *  # noqa: F401,F403

import navig_media_core.media.loudness as _impl

_sys.modules[__name__] = _impl
