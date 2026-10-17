"""The one place the media packages ask whether ffmpeg is there.

Every media module used to carry its own ``shutil.which("ffmpeg") is not None`` (three in this
package alone). ``audio_edit``, ``frames`` and ``video_edit`` re-export this one, so their
public ``ffmpeg_available`` names keep working.
"""

from __future__ import annotations

import shutil


def ffmpeg_available() -> bool:
    """True when the ffmpeg binary is on PATH."""
    return shutil.which("ffmpeg") is not None


def ffprobe_available() -> bool:
    """True when ffprobe (it ships with ffmpeg) is on PATH."""
    return shutil.which("ffprobe") is not None
