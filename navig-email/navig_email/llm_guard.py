"""The mailroom's model door — ``navig.llm.guard`` (shared with `navig paperwork`, in navig-cabinet).

Kept as a module so ``from navig_email import llm_guard`` keeps working unchanged.

**Monkeypatching this shim only reaches names the caller looks up HERE.** Call sites say
``llm_guard.generate(...)``, so patching ``llm_guard.generate`` works. Patching
``llm_guard.resolve`` does **not** change anything: ``generate`` is the very same function
object as ``navig.llm.guard.generate`` and resolves ``resolve`` in *its own* module globals.
To steer what ``generate`` resolves to, patch ``navig.llm.guard.resolve`` instead.

**Without navig** the guard is absent on purpose, not replaced. It is a privacy gate: it
refuses to send private mail to a cloud model unless the operator allowed it. A standalone
stand-in that quietly used "whatever AI is configured" would drop exactly that protection,
so AI summaries and follow-up drafts say they need navig instead (``CloudRefused``).
Everything else in the mailroom works on its own.
"""

from __future__ import annotations

try:
    from navig.llm.guard import (  # noqa: F401 — re-exports
        LOCAL_PROVIDERS,
        CloudRefused,
        Resolved,
        ensure_allowed,
        generate,
        resolve,
    )
except ImportError:
    from typing import Any

    LOCAL_PROVIDERS: frozenset[str] = frozenset()  # type: ignore[no-redef]
    try:
        import navig as _navig  # noqa: F401
    except ImportError:
        _how = "Install navig to use it: pip install navig"
    else:  # navig is here but predates navig.llm.guard
        _how = "Your navig predates it: pip install -U navig"
    _WHY = (
        "AI over your mail goes through navig's privacy guard (it refuses cloud models for "
        "private mail unless you allowed them). " + _how
    )

    class CloudRefused(RuntimeError):  # type: ignore[no-redef]
        """No AI is available here without navig's privacy guard."""

    class Resolved:  # type: ignore[no-redef]
        pass

    def resolve(*_a: Any, **_k: Any) -> Any:  # type: ignore[no-redef]
        raise CloudRefused(_WHY)

    def ensure_allowed(*_a: Any, **_k: Any) -> Any:  # type: ignore[no-redef]
        raise CloudRefused(_WHY)

    def generate(*_a: Any, **_k: Any) -> Any:  # type: ignore[no-redef]
        raise CloudRefused(_WHY)


__all__ = ["LOCAL_PROVIDERS", "CloudRefused", "Resolved", "ensure_allowed", "generate", "resolve"]
