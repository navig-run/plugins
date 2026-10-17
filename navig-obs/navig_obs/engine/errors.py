"""Exceptions for the obs-websocket engine.

The split matters for exit honesty: a *connection* problem (OBS not running,
websocket server disabled) is a different failure from a *rejected* auth and
from a *rejected request*, and the CLI reports each differently.
"""

from __future__ import annotations


class ObsError(Exception):
    """Base class for every engine failure."""


class ObsConnectionError(ObsError):
    """Cannot reach OBS (not running, wrong host/port, websocket disabled,
    connection dropped mid-request)."""


class ObsAuthError(ObsConnectionError):
    """OBS answered but rejected the handshake (wrong/missing password)."""


class ObsRequestError(ObsError):
    """OBS answered the request with ``requestStatus.result = false``."""

    def __init__(self, request_type: str, code: int | None, comment: str | None) -> None:
        self.request_type = request_type
        self.code = code
        self.comment = comment or "request failed"
        super().__init__(f"{request_type}: {self.comment} (code {code})")
