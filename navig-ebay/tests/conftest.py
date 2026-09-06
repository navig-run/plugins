"""Test config for navig-ebay.

The plugin is normally installed editable (via core's [tool.uv.sources]); this
shim also lets the tests run from a bare checkout by putting the package root on
sys.path.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parents[1]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))
