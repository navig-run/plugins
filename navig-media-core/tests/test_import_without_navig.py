"""Guard: every module here imports with ``navig`` ABSENT.

The whole reason this package exists as its own distribution is that the shared media engine
runs WITHOUT navig — navig's own services (active space, structured logger, global config)
are reached through function-local, guarded imports. An import of ``navig.*`` at module scope
would pass every in-repo test (navig is always installed in the monorepo) and then raise
``ModuleNotFoundError`` for a user who installed only this package — the exact gap
``core/tests/quality/test_standalone_plugins_run_without_navig.py`` closes for standalone
plugins, minus the console-script probe: a library has no CLI to run, so importing every
module IS the probe.

Runs in a fresh interpreter with a finder that refuses ``navig`` — first proving the refusal
works, or the test would pass with navig quietly importable. ``navig_sdk`` is a separate
package (``navig_``, not ``navig.``) and stays importable, as it is for a real user.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parents[1]
_SDK = _PLUGIN.parents[1] / "registry" / "sdk" / "python"

_PROBE = textwrap.dedent(
    """
    import importlib, importlib.abc, pkgutil, sys

    class _NoNavig(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name == "navig" or name.startswith("navig."):
                raise ModuleNotFoundError(f"No module named {name!r} (blocked: library check)", name=name)
            return None

    sys.meta_path.insert(0, _NoNavig())
    for m in [m for m in sys.modules if m == "navig" or m.startswith("navig.")]:
        del sys.modules[m]
    try:
        import navig  # noqa: F401
    except ModuleNotFoundError:
        pass
    else:
        print("BLOCK-FAILED: navig is still importable"); sys.exit(97)

    import navig_media_core
    mods = [m.name for m in pkgutil.walk_packages(navig_media_core.__path__, "navig_media_core.")]
    for name in mods:
        __import__(name)
    print(f"OK {len(mods)} submodules")
    """
)


def test_every_module_imports_with_navig_absent() -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(_PLUGIN), str(_SDK), env.get("PYTHONPATH", "")])
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
        check=False,
    )
    out = (proc.stdout + proc.stderr).strip()
    assert proc.returncode != 97, out
    assert proc.returncode == 0, (
        "a module in navig_media_core imported navig at import time (or failed to import) "
        f"with navig absent (exit {proc.returncode}):\n{out[-2500:]}"
    )
    m = re.search(r"OK (\d+) submodules", out)
    assert m, f"probe reported no module count:\n{out[-2500:]}"
    # Anti-vacuity: a walk that silently finds nothing looks exactly like a clean pass.
    assert int(m.group(1)) >= 10, f"only {m.group(1)} submodules walked — the tree moved"
