"""navig-dedupe — standalone-first perceptual duplicate finder.

Four pure engines (import the submodule you need; nothing here touches navig, and
this package root stays numpy-free so it's cheap to import at gateway boot):

    from navig_dedupe import image   # 256-bit dHash perceptual clustering + thumbnail pass
    from navig_dedupe import file    # exact SHA-256 dedupe of any file type (stdlib only)
    from navig_dedupe import audio   # Chromaprint acoustic fingerprints (needs `fpcalc`)
    from navig_dedupe import video   # keyframe dHash signatures (needs `ffmpeg`/`ffprobe`)

All engines are non-destructive: callers *quarantine* extras (move), never delete.
The ``navig-dedupe`` / ``ndup`` CLI (and, inside navig, ``navig dedupe``) drive them.

Note: engines are intentionally NOT eager-imported here — ``import navig_dedupe``
must not pull numpy/Pillow, because ``navig_dedupe.plugin`` is loaded at navig
gateway boot. Import the submodule you actually use.
"""
from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
