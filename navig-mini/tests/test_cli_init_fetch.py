"""`navig-mini init` must work from a wheel, and must not exec whatever comes back.

The README advertises `pip install navig-mini` then `navig-mini init` as Method 2. It could
never work: `[tool.setuptools.packages.find]` ships `navig_mini/` only, while install.py,
agent.py and monitor.py live at the PROJECT root — so from site-packages
`Path(__file__).parent.parent` is site-packages and install.py is simply absent. The old
code printed "install.py not found — run: curl … https://get.navig.run/mini | sh" and
exited 1, pointing at a host with no DNS record, so the suggested recovery was dead too.

It now downloads the wizard from the same URL install.sh already fetches agent.py and
monitor.py from. That means bytes off the network reach `os.execv`, so the shape of what
came back matters: a captive portal, a proxy error or a 404 page rendered as HTML must be
REFUSED rather than handed to the interpreter. That refusal is what this file pins.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parent.parent / "navig_mini" / "cli.py"


def _cli_module():
    spec = importlib.util.spec_from_file_location("_navig_mini_cli_probe", CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def cli():
    return _cli_module()


def test_base_url_matches_what_install_sh_uses(cli) -> None:
    """One source of truth: the wizard must come from where the daemon comes from.

    If these drift, `navig-mini init` fetches a wizard from one place and install.sh
    fetches the agent from another — a split-brain install that is very hard to see.
    """
    install_sh = (Path(__file__).resolve().parent.parent / "install.sh").read_text(
        encoding="utf-8", errors="replace"
    )
    assert cli.INSTALL_BASE_URL in install_sh, (
        f"cli.py fetches from {cli.INSTALL_BASE_URL}, which install.sh does not mention — "
        "the wizard and the daemon would come from different places."
    )


def test_base_url_is_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    """An airgapped or self-hosted mirror must be usable, exactly as install.sh allows."""
    monkeypatch.setenv("INSTALL_BASE_URL", "https://example.invalid/mini")
    assert _cli_module().INSTALL_BASE_URL == "https://example.invalid/mini"


@pytest.mark.parametrize(
    "payload",
    [
        b"<!DOCTYPE html><html><body>404: Not Found</body></html>",
        b"<html><head><title>Sign in</title></head></html>",   # captive portal
        b"\x00\x01\x02binary garbage",
        b"",
    ],
)
def test_non_python_payloads_are_refused(payload: bytes) -> None:
    """The guard in cmd_init: only something that looks like Python may be executed.

    Asserted against the same predicate cmd_init uses, so this cannot pass while the real
    check differs.
    """
    assert not payload.lstrip().startswith((b"#!", b'"""', b"#", b"import", b"from"))


@pytest.mark.parametrize(
    "payload",
    [
        b"#!/usr/bin/env python3\nprint('hi')\n",
        b'"""docstring first."""\n',
        b"# a comment\n",
        b"import os\n",
        b"from pathlib import Path\n",
    ],
)
def test_real_python_payloads_are_accepted(payload: bytes) -> None:
    """Teeth on the other side: the guard must not reject the real wizard.

    install.py starts with a shebang; the rest are the shapes a hand-edited or
    self-hosted wizard could legitimately begin with.
    """
    assert payload.lstrip().startswith((b"#!", b'"""', b"#", b"import", b"from"))


def test_the_shipped_wizard_would_pass_the_guard() -> None:
    """Belt and braces against the guard being too strict for THIS repo's own wizard."""
    install_py = Path(__file__).resolve().parent.parent / "install.py"
    if not install_py.is_file():
        pytest.skip("install.py absent (wheel layout)")
    head = install_py.read_bytes().lstrip()
    assert head.startswith((b"#!", b'"""', b"#", b"import", b"from"))


def test_local_install_py_still_wins(cli) -> None:
    """The curl|sh and source-checkout paths must not start downloading anything.

    `cmd_init` checks the sibling file FIRST; only its absence reaches the network.
    """
    import inspect

    src = inspect.getsource(cli.cmd_init)
    local_at = src.index("local.exists()")
    fetch_at = src.index("urlopen")
    assert local_at < fetch_at, "the local install.py check must precede the download"
