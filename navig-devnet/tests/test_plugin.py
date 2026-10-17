"""Plugin registration — needs navig (the agent/registry live there), so it is
skipped cleanly in a navig-free checkout. Proves the module registers on the
exact surfaces the per-space gates read (`cli:devnet`, `toolset:devnet`)."""

from __future__ import annotations

import pytest

pytest.importorskip("navig.modules.registry", reason="plugin registration needs navig")


def test_register_adds_devnet_module_with_gate_surfaces():
    import navig_devnet.plugin as plug
    from navig.modules.registry import ModuleRegistry

    plug._REGISTERED = False  # allow re-register in-test (module-global guard)
    plug.register()

    d = plug._devnet_module_def()
    assert d.id == "devnet"
    assert d.capability is None  # free
    # The two surfaces the per-space CLI + agent-tool gates key on:
    assert "cli:devnet" in d.surfaces
    assert "toolset:devnet" in d.surfaces

    # Module id == CLI verb, so the per-space CLI gate (match-by-name) scopes it.
    reg = ModuleRegistry().discover()
    reg._defs[d.id] = d
    assert reg.disabled_surfaces("cli", space_dir=None) is not None  # callable/no raise


def test_register_is_idempotent():
    import navig_devnet.plugin as plug

    plug._REGISTERED = False
    plug.register()
    plug.register()  # second call must be a no-op, not raise
    assert plug._REGISTERED is True
