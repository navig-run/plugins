"""obs-websocket v5 engine for navig-obs (pure stdlib + websockets, no navig import)."""

from navig_obs.engine.config import ObsTarget, resolve_target
from navig_obs.engine.errors import ObsAuthError, ObsConnectionError, ObsError, ObsRequestError

__all__ = [
    "ObsAuthError",
    "ObsConnectionError",
    "ObsError",
    "ObsRequestError",
    "ObsTarget",
    "resolve_target",
]
