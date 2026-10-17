"""Smoke tests for navig-devhost — the package imports and its surface exists."""

from __future__ import annotations

from pathlib import Path


def test_command_app_exists_with_all_commands():
    from navig_devhost.commands.devhost import devhost_app

    assert devhost_app.info.name == "devhost"
    # add · list · up · status · remove · doctor
    assert len(devhost_app.registered_commands) >= 6


def test_plugin_register_is_importable():
    from navig_devhost import plugin

    assert hasattr(plugin, "register")


def test_skill_descriptor_ships():
    """The capability descriptor exists so the agent can intent-route to devhost
    (picked up by core's plugin_capability_dirs → skills loader)."""
    import navig_devhost

    skill = Path(navig_devhost.__file__).parent / "skills" / "devhost" / "SKILL.md"
    assert skill.exists()
