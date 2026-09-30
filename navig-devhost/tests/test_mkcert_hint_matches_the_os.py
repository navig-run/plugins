"""`navig devhost doctor` told Linux and macOS users to run `winget`."""

from __future__ import annotations

import sys

import pytest

from navig_devhost.engine import certs


@pytest.mark.parametrize(
    "platform,needle",
    [("win32", "winget"), ("darwin", "brew install mkcert"), ("linux", "apt install mkcert")],
)
def test_the_install_hint_is_for_this_os(monkeypatch, platform, needle) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    hint = certs.install_hint()
    assert needle in hint
    if platform != "win32":
        assert "winget" not in hint
