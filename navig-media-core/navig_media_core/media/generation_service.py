"""Deprecated path — the generation service moved to ``navig_generate.media.generation_service``.

Generation orchestration is owned by navig-generate (ADR-0002), so this library no longer
imports a sibling plugin. Kept as an identity alias so existing imports, and tests that patch
``navig_media_core.media.generation_service.<name>``, still reach the one real module. This is
the ONLY module in navig-media-core allowed to import navig_generate (the layering test
exempts it by name); new code imports ``navig_generate.media.generation_service``.
"""

from __future__ import annotations

import sys as _sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - static analysis only
    from navig_generate.media.generation_service import *  # noqa: F401,F403

try:
    import navig_generate.media.generation_service as _impl
except ImportError as exc:
    raise ImportError(
        "The media generation service lives in navig-generate (>=0.4.0). "
        "Install it with: pip install navig-generate"
    ) from exc

_sys.modules[__name__] = _impl
