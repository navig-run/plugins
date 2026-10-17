"""Import THIS tree's packages first — worktree truth over the installed copies.

Same rationale as navig-media-core's conftest: `navig` core is installed
*editable* against whichever checkout last ran pip, and `navig-social` may be a
stale site-packages copy. Tests must exercise THIS checkout (store schema v2,
approval gate, content_hash), so core + plugin + the vendored sdk are put on
``sys.path`` before anything is imported.

The second duty is DATA isolation: the publish path writes through data-dir
singletons (receipt ledger, catalog, engagement) that resolve via
``paths.data_dir()`` — without the autouse fixture below a test run would
create/migrate the PRODUCTION stores and append live rows. NAVIG_DATA_DIR is
the documented isolation variable (never NAVIG_HOME), read per call; the
receipts singleton is reset alongside it so a cached store cannot outlive its
temp directory. Function scope: ledger rows must not bleed between tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
_PLUGIN = _TESTS.parent                  # plugins/navig-social
_REPO = _PLUGIN.parents[1]               # worktree/repo root

for _dep in (_REPO / "registry" / "sdk" / "python" / "navig_sdk",):
    if (_dep / "__init__.py").exists():
        sys.path.insert(0, str(_dep.parent))
if (_REPO / "core" / "navig" / "__init__.py").exists():
    sys.path.insert(0, str(_REPO / "core"))
if (_PLUGIN / "navig_social" / "__init__.py").exists():
    sys.path.insert(0, str(_PLUGIN))


import pytest  # noqa: E402  — after the sys.path pinning above


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVIG_DATA_DIR", str(tmp_path / "data"))
    import navig_social.social.receipts as R

    monkeypatch.setattr(R, "_store", None)  # drop any cached singleton
    yield
    # teardown: monkeypatch restores the env and the original singleton binding
