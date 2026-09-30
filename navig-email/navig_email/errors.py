"""The connector error and action types the mailroom speaks — navig's own when navig is installed.

The mailroom talks to Gmail through either navig's OAuth connector (inside navig) or its own
Gmail-over-IMAP backend (``navig_email.imap``, on its own). Callers catch
``ConnectorAPIError`` and read ``.status_code`` (the watch falls back to a time-window search
on 400/404), and build ``Action``/``ActionType`` for send/reply. With navig present these ARE
navig's classes, so an ``except`` written here catches what navig's connector raises; without
navig they are same-shaped standalone classes the IMAP backend raises and accepts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = ["Action", "ActionResult", "ActionType", "ConnectorAPIError", "ConnectorAuthError"]

try:
    from navig.connectors.errors import ConnectorAPIError, ConnectorAuthError
except ImportError:

    class _ConnectorError(Exception):
        def __init__(self, connector_id: str, message: str = "") -> None:
            self.connector_id = connector_id
            super().__init__(f"[{connector_id}] {message}" if message else f"[{connector_id}]")

    class ConnectorAPIError(_ConnectorError):  # type: ignore[no-redef]
        """The mail server refused a request (``status_code`` mirrors navig's HTTP-style codes)."""

        def __init__(self, connector_id: str, status_code: int, detail: str = "") -> None:
            self.status_code = status_code
            self.detail = detail
            super().__init__(connector_id, f"API error {status_code}" + (f": {detail}" if detail else ""))

    class ConnectorAuthError(_ConnectorError):  # type: ignore[no-redef]
        """The credentials were refused."""


try:
    from navig.connectors.types import Action, ActionResult, ActionType
except ImportError:

    class ActionType(str, Enum):  # type: ignore[no-redef]
        REPLY = "reply"
        CREATE = "create"
        UPDATE = "update"
        DELETE = "delete"
        ARCHIVE = "archive"
        LABEL = "label"
        SEND = "send"
        MOVE = "move"

    @dataclass
    class Action:  # type: ignore[no-redef]
        action_type: ActionType
        connector_id: str | None = None
        resource_id: str | None = None
        params: dict[str, Any] = field(default_factory=dict)

    @dataclass
    class ActionResult:  # type: ignore[no-redef]
        success: bool
        resource: Any = None
        error: str | None = None

        def to_dict(self) -> dict[str, Any]:
            payload: dict[str, Any] = {"success": self.success}
            if self.error:
                payload["error"] = self.error
            return payload
