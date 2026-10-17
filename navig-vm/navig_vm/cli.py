"""Standalone entry point for the ``navig-vm`` console script.

Runs without navig: the same ``vm_app`` navig mounts as ``navig vm``.
"""

from __future__ import annotations

from navig_sdk.cli import run

from navig_vm.commands.vm import vm_app


def main() -> None:
    run(vm_app, "navig-vm")


if __name__ == "__main__":
    main()
