"""Import THIS tree's packages first.

navig-generate, navig-sdk and navig are installed from the MAIN checkout (or PyPI), so in a
worktree the tests would import those rather than the code under test -- and core's
``navig.media.*`` / ``navig.tools.*_generation`` are aliases of this package's modules.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parents[1]
_REPO = _PLUGIN.parents[1]

for _dep in (_REPO / "registry" / "sdk" / "python" / "navig_sdk", _REPO / "core" / "navig"):
    if (_dep / "__init__.py").exists():
        sys.path.insert(0, str(_dep.parent))
sys.path.insert(0, str(_PLUGIN))
