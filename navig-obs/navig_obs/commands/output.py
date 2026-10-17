"""`navig obs` output groups: stream · record · replay · vcam · studio.

The stream key is a SECRET: `stream settings` masks it in every rendering
(including --json); `stream settings --set` is the only way to write it.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer

from navig_obs.commands._common import console, emit, fail, kv_table, ok, request

# StartStream / StartRecord only QUEUE a start: OBS answers 200 and may then
# fail to begin (an encoder already initialised, a full disk, a bad path).
# 2026-10-16: `record start` printed "recording started" while OBS logged
# "Cannot apply a new video_t object after the encoder has been initialized"
# and recorded nothing. So the start is confirmed before it is announced.
START_CONFIRM_S = 6.0
START_POLL_S = 0.25


def _confirm_started(status_request: str, what: str) -> None:
    import time as _time

    deadline = _time.monotonic() + START_CONFIRM_S
    while True:
        if (request(status_request) or {}).get("outputActive"):
            return
        if _time.monotonic() >= deadline:
            fail(
                f"OBS accepted the start but is not {what} after {START_CONFIRM_S:.0f}s - "
                "its own log says why (an encoder already initialised, a full disk, a "
                "bad output path); restarting OBS clears a stuck encoder. "
                "`navig obs doctor` checks the rest."
            )
        _time.sleep(START_POLL_S)

stream_app = typer.Typer(name="stream", help="Stream output: status, start/stop, settings.", no_args_is_help=True)
record_app = typer.Typer(name="record", help="Recording: status, start/stop, directory.", no_args_is_help=True)
replay_app = typer.Typer(name="replay", help="Replay buffer: status, start/stop.", no_args_is_help=True)
vcam_app = typer.Typer(name="vcam", help="Virtual camera: status, start/stop.", no_args_is_help=True)
studio_app = typer.Typer(name="studio", help="Studio mode: status, enable/disable.", no_args_is_help=True)


def _masked(data: dict[str, Any]) -> dict[str, Any]:
    """Copy with every secret-looking value replaced (stream keys, tokens)."""
    out = dict(data)
    for key in list(out):
        if key.lower() in ("streamkey", "key", "password", "token", "auth"):
            if out[key]:
                out[key] = "********"
    settings = out.get("streamSettings")
    if isinstance(settings, dict):
        out["streamSettings"] = _masked(settings)
    return out


# ── stream ───────────────────────────────────────────────────────────────────
@stream_app.command("status")
def stream_status(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Live stream state: active, duration, frames, bitrate."""
    data = request("GetStreamStatus")

    def _render(d: dict) -> None:
        active = bool(d.get("outputActive"))
        ms = int(d.get("outputDuration") or 0)
        console.print(
            kv_table(
                "stream",
                [
                    ("state", "[green]● live[/green]" if active else "[dim]○ idle[/dim]"),
                    ("duration", f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d}"),
                    ("bytes sent", str(d.get("outputBytes", "—"))),
                    ("frames", str(d.get("outputFrames", "—"))),
                    ("skipped frames", str(d.get("outputSkippedFrames", "—"))),
                    ("congestion", str(d.get("outputCongestion", "—"))),
                ],
            )
        )

    emit(data, _render, as_json=as_json)


@stream_app.command("start")
def stream_start(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Go live."""
    data = request("StartStream")
    _confirm_started("GetStreamStatus", "streaming")
    emit(data, as_json=as_json)
    if not as_json:
        ok("stream started")


@stream_app.command("stop")
def stream_stop(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Stop streaming."""
    emit(request("StopStream"), as_json=as_json)
    if not as_json:
        ok("stream stopped")


@stream_app.command("pause")
def stream_pause(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Pause the stream output (RTMP pause support depends on the service)."""
    emit(request("PauseStream"), as_json=as_json)
    if not as_json:
        ok("stream paused")


@stream_app.command("resume")
def stream_resume(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Resume a paused stream."""
    emit(request("ResumeStream"), as_json=as_json)
    if not as_json:
        ok("stream resumed")


@stream_app.command("settings")
def stream_settings(
    set_server: Annotated[str | None, typer.Option("--set-server", help="RTMP server URL to save.")] = None,
    set_key: Annotated[str | None, typer.Option("--set-key", help="Stream key to save (never printed).")] = None,
    type_: Annotated[str, typer.Option("--type", help="rtmp_custom | rtmp_common.")] = "rtmp_custom",
    reveal: Annotated[bool, typer.Option("--reveal", help="Show the stream key instead of masking.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Read (masked) or write the stream destination. Key never prints unless --reveal."""
    if set_server is None and set_key is None:
        data = request("GetStreamSettings", {"streamServiceType": type_})
        if reveal:
            emit(data, as_json=as_json)
            return
        masked = _masked(data)

        def _render(d: dict) -> None:
            st = d.get("streamSettings") or {}
            # rtmp_custom spells it "key"; rtmp_common uses streamKey — accept both.
            key = st.get("streamKey") or st.get("key")
            console.print(
                kv_table(
                    "stream settings",
                    [
                        ("type", str(d.get("streamServiceType", "—"))),
                        ("server", str(st.get("server") or st.get("streamUrl") or "—")),
                        ("key", "********" if key else "—"),
                        ("site", str(st.get("site", "—"))),
                    ],
                )
            )
            console.print("[dim]stream key masked — `--reveal` to show it[/dim]")

        emit(masked, _render, as_json=as_json)
        return

    # Read-modify-write: fetch current settings, apply the provided fields.
    current = request("GetStreamSettings", {"streamServiceType": type_})
    settings = dict(current.get("streamSettings") or {})
    if set_server is not None:
        settings["server"] = set_server
    if set_key is not None:
        settings["key"] = set_key
    request(
        "SetStreamSettings",
        {"streamServiceType": type_, "streamSettings": settings, "save": True},
    )
    ok("stream settings saved" + (" (key updated)" if set_key is not None else ""))


# ── record ───────────────────────────────────────────────────────────────────
@record_app.command("status")
def record_status(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Recording state: active, duration, paused."""
    data = request("GetRecordStatus")

    def _render(d: dict) -> None:
        active = bool(d.get("outputActive"))
        ms = int(d.get("outputDuration") or 0)
        console.print(
            kv_table(
                "record",
                [
                    ("state", "[green]● recording[/green]" if active else "[dim]○ idle[/dim]"),
                    ("paused", "yes" if d.get("outputPaused") else "no"),
                    ("duration", f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d}"),
                    ("bytes", str(d.get("outputBytes", "—"))),
                ],
            )
        )

    emit(data, _render, as_json=as_json)


@record_app.command("start")
def record_start(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Start recording."""
    data = request("StartRecord")
    _confirm_started("GetRecordStatus", "recording")
    emit(data, as_json=as_json)
    if not as_json:
        ok("recording started")


@record_app.command("stop")
def record_stop(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Stop recording (returns the saved file path)."""
    emit(request("StopRecord"), as_json=as_json)
    if not as_json:
        ok("recording stopped")


@record_app.command("pause")
def record_pause(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Pause an active recording."""
    emit(request("PauseRecord"), as_json=as_json)
    if not as_json:
        ok("recording paused")


@record_app.command("resume")
def record_resume(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Resume a paused recording."""
    emit(request("ResumeRecord"), as_json=as_json)
    if not as_json:
        ok("recording resumed")


@record_app.command("directory")
def record_directory(
    set_path: Annotated[str | None, typer.Option("--set", help="New recording directory.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Get (default) or set the recording output directory."""
    if set_path:
        emit(request("SetRecordDirectory", {"recordDirectory": set_path}), as_json=as_json)
        if not as_json:
            ok(f"record directory → {set_path}")
        return
    emit(request("GetRecordDirectory"), as_json=as_json)


# ── replay buffer ────────────────────────────────────────────────────────────
@replay_app.command("status")
def replay_status(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Replay buffer state (saved time, active)."""
    data = request("GetReplayBufferStatus")

    def _render(d: dict) -> None:
        console.print(
            kv_table(
                "replay buffer",
                [
                    ("state", "[green]● on[/green]" if d.get("outputActive") else "[dim]○ off[/dim]"),
                    ("saved time (ms)", str(d.get("outputTime", "—"))),
                ],
            )
        )

    emit(data, _render, as_json=as_json)


@replay_app.command("start")
def replay_start(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Start the replay buffer."""
    emit(request("StartReplayBuffer"), as_json=as_json)
    if not as_json:
        ok("replay buffer started")


@replay_app.command("stop")
def replay_stop(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Stop the replay buffer."""
    emit(request("StopReplayBuffer"), as_json=as_json)
    if not as_json:
        ok("replay buffer stopped")


@replay_app.command("save")
def replay_save(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Save the replay buffer to disk now."""
    emit(request("SaveReplayBuffer"), as_json=as_json)
    if not as_json:
        ok("replay saved")


# ── virtual camera ───────────────────────────────────────────────────────────
@vcam_app.command("status")
def vcam_status(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Virtual camera state."""
    data = request("GetVirtualCamStatus")

    def _render(d: dict) -> None:
        console.print(
            kv_table(
                "virtual camera",
                [
                    ("state", "[green]● on[/green]" if d.get("outputActive") else "[dim]○ off[/dim]"),
                    ("duration (ms)", str(d.get("outputDuration", "—"))),
                ],
            )
        )

    emit(data, _render, as_json=as_json)


@vcam_app.command("start")
def vcam_start(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Start the virtual camera."""
    emit(request("StartVirtualCam"), as_json=as_json)
    if not as_json:
        ok("virtual camera started")


@vcam_app.command("stop")
def vcam_stop(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Stop the virtual camera."""
    emit(request("StopVirtualCam"), as_json=as_json)
    if not as_json:
        ok("virtual camera stopped")


# ── studio mode ──────────────────────────────────────────────────────────────
@studio_app.command("status")
def studio_status(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Studio mode state + preview/program scenes."""
    enabled = request("GetStudioModeEnabled")
    preview = request("GetCurrentPreviewScene") if enabled.get("studioModeEnabled") else {}
    program = request("GetCurrentProgramScene")

    def _render(_: dict) -> None:
        on = bool(enabled.get("studioModeEnabled"))
        rows: list[tuple[str, str]] = [
            ("state", "[green]● on[/green]" if on else "[dim]○ off[/dim]"),
            ("program", str(program.get("currentProgramSceneName", "—"))),
        ]
        if on:
            rows.append(("preview", str(preview.get("currentPreviewSceneName", "—"))))
        console.print(kv_table("studio mode", rows))

    combined = {"enabled": enabled, "preview": preview, "program": program}
    emit(combined, _render, as_json=as_json)


@studio_app.command("enable")
def studio_enable(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Turn studio mode on."""
    emit(request("SetStudioModeEnabled", {"studioModeEnabled": True}), as_json=as_json)
    if not as_json:
        ok("studio mode on")


@studio_app.command("disable")
def studio_disable(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Turn studio mode off."""
    emit(request("SetStudioModeEnabled", {"studioModeEnabled": False}), as_json=as_json)
    if not as_json:
        ok("studio mode off")


@studio_app.command("to-program")
def studio_to_program(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Transition the preview scene to program (studio mode)."""
    emit(request("TriggerStudioModeTransition"), as_json=as_json)
    if not as_json:
        ok("preview → program")
