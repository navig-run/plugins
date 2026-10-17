"""Import THIS tree's packages first.

navig-media-core and navig-sdk are installed from the MAIN checkout (or PyPI), so in a
worktree the tests would import those rather than the code under test.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parents[1]
_REPO = _PLUGIN.parents[1]

for _dep in (_REPO / "registry" / "sdk" / "python" / "navig_sdk",):
    if (_dep / "__init__.py").exists():
        sys.path.insert(0, str(_dep.parent))
sys.path.insert(0, str(_PLUGIN))
