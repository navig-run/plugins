"""What the show does to OBS — a few obs-websocket v5 moves, nothing else.

``client`` is anything with ``request(type, data) -> dict`` (an
:class:`~navig_obs.engine.client.ObsClient`, or a recorder in tests).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from navig_obs.show.cues import Cue

MONITOR_AND_OUTPUT = "OBS_MONITORING_TYPE_MONITOR_AND_OUTPUT"
RESTART = "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART"


class Requester(Protocol):
    def request(
        self, request_type: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...


def play_cue(client: Requester, cue_input: str, cue: Cue) -> None:
    """Load the cue into the ONE cue input and play it from the start.

    One input = one playback path (contract-04): a second cue replaces the
    first instead of stacking on top of it.
    """
    play_file(client, cue_input, cue.path)


def play_file(client: Requester, cue_input: str, path: Path | str) -> None:
    """Point the ONE cue input at ``path`` and play it from the start.

    The shared move behind a cue and a spoken line (``show say``): both go
    through the same input, so a line replaces a cue instead of stacking.
    """
    client.request(
        "SetInputSettings",
        {
            "inputName": cue_input,
            "inputSettings": {"is_local_file": True, "local_file": str(path)},
            "overlay": True,
        },
    )
    client.request(
        "TriggerMediaInputAction", {"inputName": cue_input, "mediaAction": RESTART}
    )


def emit_event(client: Requester, event_name: str, data: dict[str, Any]) -> None:
    """Hand a page event to every browser source (``window`` sees
    ``event_name`` with ``data`` as its detail). The overlay decides what to
    do with it; nothing here draws anything."""
    client.request(
        "CallVendorRequest",
        {
            "vendorName": "obs-browser",
            "requestType": "emit_event",
            "requestData": {"event_name": event_name, "event_data": data},
        },
    )


def emit_toast(client: Requester, event_name: str, toast: dict[str, Any]) -> None:
    """Hand a toast to every browser source (the overlay draws it, in-world)."""
    emit_event(client, event_name, toast)


def ensure_cue_input(
    client: Requester, cue_input: str, scenes: list[str]
) -> dict[str, list[str]]:
    """Create the cue input once and place it in each scene (idempotent).

    A media source only sounds while it is in the live scene, so the same
    input is added to every scene that may be on air — still one input, one
    playback path. Monitoring = monitor AND output, so the operator hears it.
    """
    created: list[str] = []
    placed: list[str] = []
    inputs = {
        i.get("inputName") for i in (client.request("GetInputList").get("inputs") or [])
    }
    for scene in scenes:
        if cue_input not in inputs:
            client.request(
                "CreateInput",
                {
                    "sceneName": scene,
                    "inputName": cue_input,
                    "inputKind": "ffmpeg_source",
                    "inputSettings": {
                        "is_local_file": True,
                        "looping": False,
                        "restart_on_activate": False,
                    },
                    "sceneItemEnabled": True,
                },
            )
            client.request(
                "SetInputAudioMonitorType",
                {"inputName": cue_input, "monitorType": MONITOR_AND_OUTPUT},
            )
            inputs.add(cue_input)
            created.append(scene)
            continue
        items = (
            client.request("GetSceneItemList", {"sceneName": scene}).get("sceneItems")
            or []
        )
        if any(it.get("sourceName") == cue_input for it in items):
            continue
        client.request(
            "CreateSceneItem",
            {"sceneName": scene, "sourceName": cue_input, "sceneItemEnabled": True},
        )
        placed.append(scene)
    return {"created": created, "placed": placed}


def current_collection(client: Requester) -> str | None:
    """The loaded scene collection's name (it carries the stage's language)."""
    try:
        res = client.request("GetSceneCollectionList", {}) or {}
    except Exception:  # noqa: BLE001 — an unknown stage falls back to the default
        return None
    name = res.get("currentSceneCollectionName")
    return str(name) if name else None
