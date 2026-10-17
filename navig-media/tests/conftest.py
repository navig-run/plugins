from __future__ import annotations

import sys
from pathlib import Path

# The bundle is importable straight from the checkout (it is not installed in the gate).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
