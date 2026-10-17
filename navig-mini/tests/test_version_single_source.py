"""Version single-sourcing: every surface that reports a navig-mini version agrees.

`navig_mini.__version__` is the one runtime source. Four other places CANNOT import it:

* ``pyproject.toml`` — the publish source (what PyPI served as 1.1.3 on 2026-09-05);
* ``agent.py`` — a deployed box receives agent.py + monitor.py ALONE (the installer and
  ``navig mini deploy`` fetch no package), so it keeps a fallback literal;
* ``install.py`` — fetched and run standalone (``python3 <(wget …/mini.py)``);
* ``install.sh`` — shell, no Python to import from.

Each of those is therefore a copy, and a copy is exactly how the surfaces drifted
(1.1.3 published while the runtime still said 1.1.1). This module pins every copy to
``navig_mini.__version__`` so it cannot happen again.

Files are read as TEXT on purpose: ``install.py`` and ``install.sh`` must stay
standalone, and importing install.py to read one constant would hide the very thing
being checked — that the literal on disk is the right one.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import navig_mini

ROOT = Path(__file__).resolve().parent.parent

_AGENT_VERSION = re.compile(r'^\s*VERSION\s*=\s*"([^"]+)"', re.MULTILINE)
_INSTALL_PY_VERSION = re.compile(r'^\s*VERSION\s*=\s*"([^"]+)"', re.MULTILINE)
_INSTALL_SH_VERSION = re.compile(r'^NAVIG_MINI_VERSION="([^"]+)"', re.MULTILINE)


def _text(name: str) -> str:
    path = ROOT / name
    assert path.is_file(), f"{name} is missing from the plugin root"
    return path.read_text(encoding="utf-8")


def _one(pattern: re.Pattern[str], name: str) -> str:
    match = pattern.search(_text(name))
    assert match, f"{name} no longer declares its version literal — the pin needs a new regex"
    return match.group(1)


def _pyproject_version() -> str:
    return tomllib.loads(_text("pyproject.toml"))["project"]["version"]


def test_pyproject_version_is_the_single_source():
    assert _pyproject_version() == navig_mini.__version__


def test_package_init_has_no_imports():
    """The single source must stay import-free — a Pi importing it pulls nothing in."""
    body = _text("navig_mini/__init__.py")
    assert not re.search(r"^\s*(?:import|from)\s+\S", body, re.MULTILINE)


def test_agent_version_fallback_literal_matches():
    """``agent.py``'s standalone fallback must equal ``navig_mini.__version__``.

    Regexed rather than imported so a broken derivation (the import removed, the
    literal left behind) is still caught at the value that a deployed box reports.
    """
    assert _one(_AGENT_VERSION, "agent.py") == navig_mini.__version__


def test_agent_derives_the_version_when_the_package_is_present():
    """From a checkout the daemon must report the package's version, not its copy."""
    import agent

    assert agent.VERSION == navig_mini.__version__


def test_install_py_version_matches():
    assert _one(_INSTALL_PY_VERSION, "install.py") == navig_mini.__version__


def test_install_sh_version_matches():
    assert _one(_INSTALL_SH_VERSION, "install.sh") == navig_mini.__version__


def test_every_surface_reports_one_version():
    """The single assertion the drift history exists to make impossible."""
    declared = {
        "pyproject.toml": _pyproject_version(),
        "navig_mini/__init__.py": navig_mini.__version__,
        "agent.py": _one(_AGENT_VERSION, "agent.py"),
        "install.py": _one(_INSTALL_PY_VERSION, "install.py"),
        "install.sh": _one(_INSTALL_SH_VERSION, "install.sh"),
    }
    assert len(set(declared.values())) == 1, f"version drift across surfaces: {declared}"
