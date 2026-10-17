"""The package imports, its surface exists, and its data files ship."""

from __future__ import annotations

from pathlib import Path


def test_command_app_has_its_commands() -> None:
    from navig_vm.commands.vm import vm_app

    names = {c.name for c in vm_app.registered_commands}
    assert {"create", "start", "stop", "list", "ip", "ssh", "exec", "wait",
            "snapshot", "revert", "snapshots", "screenshot", "password", "destroy", "console"} <= names


def test_plugin_register_is_importable() -> None:
    from navig_vm import plugin

    assert hasattr(plugin, "register")


def test_recipes_and_skill_ship() -> None:
    import navig_vm
    from navig_vm.engine import seed

    assert {"plain", "navig-os"} <= set(seed.recipe_names())
    assert (Path(navig_vm.__file__).parent / "skills" / "vm" / "SKILL.md").is_file()
