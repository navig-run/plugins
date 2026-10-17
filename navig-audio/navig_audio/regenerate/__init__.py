"""AI regeneration — new audio made from an existing recording, always labelled as new.

See :mod:`navig_audio.regenerate.core`. Audio Enhance (:mod:`navig_audio.enhance`) is the
opposite tool: it filters the original and proves it is still the same recording.
"""

from navig_audio.regenerate.core import (
    OPS,
    STEMS_SIX,
    ElevenLabsProvider,
    RegenerateError,
    RegenerateResult,
    regenerate,
)
from navig_audio.regenerate.description import Description, describe

__all__ = [
    "OPS", "STEMS_SIX", "Description", "ElevenLabsProvider", "RegenerateError", "RegenerateResult",
    "describe", "regenerate",
]
