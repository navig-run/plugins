"""Import THIS tree's packages first.

navig-video, navig-audio and navig-media-core are installed from the MAIN
checkout (or PyPI), so in a worktree the tests would import those rather than
the code under test — same pattern as navig-pipeline's conftest.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parents[1]
_REPO = _PLUGIN.parents[1]

for _dep in (
    _REPO / "registry" / "sdk" / "python" / "navig_sdk",
    _REPO / "plugins" / "navig-audio" / "navig_audio",
    _REPO / "plugins" / "navig-generate" / "navig_generate",
    _REPO / "plugins" / "navig-media-core" / "navig_media_core",
):
    if (_dep / "__init__.py").exists():
        sys.path.insert(0, str(_dep.parent))
sys.path.insert(0, str(_PLUGIN))
