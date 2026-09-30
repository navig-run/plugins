"""`games_claim` writes to the operator's real store account, so it must ask first.

The NAVIG approval gate decides "does this need confirmation?" from a set of core's own
tool names plus a tool's self-declared `safety`. A plugin's tools are invisible to that
set — core cannot enumerate a plugin's vocabulary — so `games_claim` ran with no
confirmation: it completes a checkout on the operator's Epic account using their vaulted
session.

`owner_only = True` is NOT the mechanism. It is an authorization flag that ~20 read-only
devops tools also set, so the gate deliberately does not read it; `safety` is what the
gate reads.

The three read tools must stay ungated — gating a read costs nothing in safety and
trains the operator to click through prompts, which is how a real prompt gets ignored.
"""

from __future__ import annotations

import pytest

from navig_games.agent_tools import (
    GamesCheckTool,
    GamesClaimTool,
    GamesDealsTool,
    GamesLibraryTool,
)


def test_claim_declares_itself_dangerous() -> None:
    assert getattr(GamesClaimTool, "safety", None) == "dangerous", (
        "games_claim completes a checkout on the operator's real store account. Without "
        "`safety = \"dangerous\"` the NAVIG approval gate has no way to know that — its "
        "name is not in core's DESTRUCTIVE_TOOLS and cannot be."
    )


@pytest.mark.parametrize("cls", [GamesCheckTool, GamesDealsTool, GamesLibraryTool])
def test_the_read_tools_do_not_declare_danger(cls) -> None:
    """Over-gating reads is its own failure mode."""
    assert getattr(cls, "safety", None) != "dangerous", cls.name


def test_the_gate_actually_holds_claim() -> None:
    """End to end through the real registry and the real predicate."""
    approval = pytest.importorskip(
        "navig.tools.approval", reason="navig core not importable"
    )
    registry = pytest.importorskip("navig.agent.agent_tool_registry")

    approval.set_approval_policy(approval.ApprovalPolicy.CONFIRM_DESTRUCTIVE)
    reg = registry.AgentToolRegistry()
    reg.register(GamesClaimTool(), toolset="games")

    assert approval.needs_approval("games_claim") is True


def test_the_gate_does_not_hold_the_read_tools() -> None:
    approval = pytest.importorskip(
        "navig.tools.approval", reason="navig core not importable"
    )
    registry = pytest.importorskip("navig.agent.agent_tool_registry")

    approval.set_approval_policy(approval.ApprovalPolicy.CONFIRM_DESTRUCTIVE)
    reg = registry.AgentToolRegistry()
    for cls in (GamesCheckTool, GamesDealsTool, GamesLibraryTool):
        reg.register(cls(), toolset="games")
        assert approval.needs_approval(cls.name) is False, cls.name
