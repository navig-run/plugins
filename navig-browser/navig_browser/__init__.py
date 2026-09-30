"""navig-browser — the browser engine navig drives, and a CLI of its own.

Launch an isolated Chrome/Edge/Brave (or an Electron app) with a debug port, drive it over
CDP, keep named profiles, fill logins from the shared vault, and stop exactly the browsers it
launched. navig mounts the CLI on ``navig cdp``; on its own it is ``navig-browser``.

``CortexOrchestrator`` (the AI page-driving loop behind ``navig cortex``) is resolved lazily:
it needs navig's AI, so importing this package never requires navig.
"""

from __future__ import annotations

from typing import Any

from .controller import BrowserConfig, BrowserController
from .router import cdp_browser, fast_browser, get_browser, stealth_browser
from .template_runner import TemplateRunner

__version__ = "0.1.0"

__all__ = [
    "BrowserController",
    "BrowserConfig",
    "get_browser",
    "fast_browser",
    "stealth_browser",
    "cdp_browser",
    "CortexOrchestrator",
    "TemplateRunner",
    "__version__",
]


def __getattr__(name: str) -> Any:
    if name == "CortexOrchestrator":
        from .orchestrator import CortexOrchestrator

        return CortexOrchestrator
    raise AttributeError(f"module 'navig_browser' has no attribute {name!r}")
