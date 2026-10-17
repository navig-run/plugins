"""The two version sources must agree — they drifted once (0.1.0 vs 0.2.0).

pyproject.toml is what the catalog/registry/pip see; ``__version__`` is what
``import navig_agentctl`` reports. A bump that touches only one ships two
different versions of the same package. Tripwire, not a lint: fail here.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import navig_agentctl

_PYPROJECT = Path(navig_agentctl.__file__).resolve().parents[1] / "pyproject.toml"


def test_version_matches_pyproject():
    assert _PYPROJECT.is_file(), f"pyproject.toml not found next to the package: {_PYPROJECT}"
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    declared = data["project"]["version"]
    assert navig_agentctl.__version__ == declared, (
        f"__version__={navig_agentctl.__version__!r} but pyproject declares "
        f"{declared!r} — bump BOTH in the same commit"
    )
