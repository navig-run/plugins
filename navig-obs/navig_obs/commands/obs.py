"""`navig obs` — control OBS Studio over obs-websocket v5.

Top-level verbs live here (status · doctor · stats · screenshot · request); resource
groups (scenes, items, sources, filters, audio, transitions, stream, record, replay,
vcam, studio, collections, profiles, media, hotkeys) are sub-apps in sibling
modules, mounted at the bottom of this file.
"""

from __future__ import annotations

import json as _json
import re
from pathlib import Path
from typing import Annotated

import typer

from navig_obs.commands._common import (
    console,
    emit,
    fail,
    grid,
    kv_table,
    notice,
    ok,
    request,
    request_optional,
    set_target,
    target,
)
from navig_obs.engine.client import EVENT_TOPICS
from navig_obs.engine.config import resolve_target
from navig_obs.engine.errors import ObsAuthError, ObsConnectionError

obs_app = typer.Typer(
    name="obs",
    help="Control OBS Studio: scenes, sources, stream, recording, studio mode (obs-websocket v5).",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


@obs_app.callback()
def _main(
    host: Annotated[str | None, typer.Option("--host", help="OBS websocket host (default 127.0.0.1).")] = None,
    port: Annotated[int | None, typer.Option("--port", help="OBS websocket port (default 4455).")] = None,
    password: Annotated[
        str | None,
        typer.Option("--password", help="Websocket password. Prefer NAVIG_OBS_PASSWORD env or obs.password config."),
    ] = None,
) -> None:
    """Connect target: flags > NAVIG_OBS_* env > navig obs.* config > local OBS file."""
    set_target(resolve_target(host=host, port=port, password=password))


# ── status ───────────────────────────────────────────────────────────────────
@obs_app.command("status")
def status(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Live OBS state: version, current scene, stream/record/replay/vcam/studio.

    An unreachable OBS is a SKIP (exit 0): "not running" is an answer, not a failure.
    """
    t = target()
    version = request_optional("GetVersion")
    if version is None:
        if as_json:
            emit({"reachable": False, "host": t.host, "port": t.port}, as_json=True)
        else:
            notice(
                f"OBS not reachable at {t.host}:{t.port} — start OBS and enable "
                "Tools → WebSocket Server Settings (`navig obs doctor` for the full check)"
            )
        raise typer.Exit(0)

    stream = request_optional("GetStreamStatus") or {}
    record = request_optional("GetRecordStatus") or {}
    replay = request_optional("GetReplayBufferStatus") or {}
    vcam = request_optional("GetVirtualCamStatus") or {}
    studio = request_optional("GetStudioModeEnabled") or {}
    scene = request_optional("GetCurrentProgramScene") or {}

    def _ms_running(d: dict) -> bool:
        return bool(d.get("outputActive"))

    state = {
        "reachable": True,
        "host": t.host,
        "port": t.port,
        "obs_version": version.get("obsVersion"),
        "websocket_version": version.get("obsWebSocketVersion"),
        "rpc_version": version.get("rpcVersion"),
        "platform": version.get("platform") or version.get("platformDescription"),
        "current_scene": scene.get("currentProgramSceneName"),
        "stream": {
            "active": _ms_running(stream),
            "duration_ms": stream.get("outputDuration"),
            "bytes": stream.get("outputBytes"),
            "delayed": stream.get("outputSkippedFrames"),
        },
        "record": {"active": _ms_running(record), "duration_ms": record.get("outputDuration")},
        "replay_buffer": {"active": _ms_running(replay)},
        "virtual_cam": {"active": _ms_running(vcam)},
        "studio_mode": bool(studio.get("studioModeEnabled")),
    }
    if as_json:
        emit(state, as_json=True)
        return

    def _on_off(active: bool) -> str:
        return "[green]● on[/green]" if active else "[dim]○ off[/dim]"

    def _duration(ms: object) -> str:
        if not isinstance(ms, (int, float)) or ms <= 0:
            return ""
        s = int(ms) // 1000
        return f" {s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"

    console.print(f"[green]●[/green] connected to {t.host}:{t.port}")
    console.print(
        kv_table(
            "OBS status",
            [
                ("obs", str(state["obs_version"] or "?")),
                ("websocket", str(state["websocket_version"] or "?")),
                ("platform", str(state["platform"] or "?")),
                ("scene (program)", state["current_scene"] or "?"),
                ("stream", _on_off(state["stream"]["active"]) + _duration(state["stream"]["duration_ms"])),
                ("record", _on_off(state["record"]["active"]) + _duration(state["record"]["duration_ms"])),
                ("replay buffer", _on_off(state["replay_buffer"]["active"])),
                ("virtual cam", _on_off(state["virtual_cam"]["active"])),
                ("studio mode", _on_off(state["studio_mode"])),
            ],
        )
    )


# ── doctor ───────────────────────────────────────────────────────────────────
@obs_app.command("doctor")
def doctor(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Local OBS health check: install, websocket config, plugin load errors, missing files.

    Read-only; runs without OBS being connected. Includes the newest OBS log's
    error lines and a table of scene-file references that no longer exist.
    """
    from navig_obs import doctor as doc

    report = doc.run_doctor(host=target().host)
    if as_json:
        emit(report.as_dict(), as_json=True)
        raise typer.Exit(1 if report.failed else 0)

    glyph = {doc.PASS: "[green]●[/green]", doc.WARN: "[yellow]![/yellow]", doc.FAIL: "[red]✗[/red]"}
    for c in report.checks:
        line = f"{glyph.get(c.state, '?')} {c.name}: {c.detail}" if c.detail else f"{glyph.get(c.state, '?')} {c.name}"
        console.print(line)
        if c.hint:
            console.print(f"    [dim]→ {c.hint}[/dim]")

    if report.log_errors:
        console.print("\n[bold]latest OBS log errors[/bold]")
        for line in report.log_errors[:15]:
            console.print(f"  [dim]{line}[/dim]")
        if len(report.log_errors) > 15:
            console.print(f"  [dim]… {len(report.log_errors) - 15} more[/dim]")

    if report.missing_files:
        console.print()
        grid(
            "missing scene files (the OBS 'Missing Files' dialog)",
            [("source", False), ("file", False), ("collection", True)],
            [[m["source"], m["file"], m["collection"]] for m in report.missing_files],
            empty="",
        )

    warns = sum(1 for c in report.checks if c.state == doc.WARN)
    if report.failed:
        fail("doctor found a failing check (see ✗ above)")
    if warns:
        notice(f"{warns} warning(s) — control verbs may fail until they are resolved")
    else:
        ok("all checks passed")


# ── stats ────────────────────────────────────────────────────────────────────
@obs_app.command("stats")
def stats(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """OBS resource stats: CPU, memory, FPS, render/output frame health."""
    data = request("GetStats")

    def _render(d: dict) -> None:
        rows: list[tuple[str, str]] = []
        for key, label in (
            ("cpuUsage", "cpu usage"),
            ("memoryUsage", "memory usage (MB)"),
            ("availableDiskSpace", "disk free (MB)"),
            ("activeFps", "active fps"),
            ("renderFps", "render fps"),
            ("renderSkippedFrames", "render skipped frames"),
            ("outputSkippedFrames", "output skipped frames"),
            ("webSocketSessionCount", "websocket sessions"),
        ):
            if key in d and d[key] is not None:
                rows.append((label, str(d[key])))
        console.print(kv_table("OBS stats", rows))

    emit(data, _render, as_json=as_json)


# ── screenshot ───────────────────────────────────────────────────────────────
def _user_path(raw: str) -> Path:
    """Operator-typed path → resolve_user_path (guarded; standalone fallback)."""
    try:
        from navig.platform.paths import resolve_user_path

        return Path(resolve_user_path(raw))
    except Exception:  # noqa: BLE001 — standalone: no navig
        return Path(raw).expanduser()


@obs_app.command("screenshot")
def screenshot(
    source: Annotated[str, typer.Argument(help="Source or scene name to capture.")],
    out: Annotated[
        str | None,
        typer.Option("--out", "-o", help="Where to save the image (default ./<source>.png)."),
    ] = None,
    fmt: Annotated[
        str, typer.Option("--format", help="png | jpg | webp | bmp (default: from --out extension).")
    ] = "",
    scene: Annotated[str | None, typer.Option("--scene", help="Scene context for group sources.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Capture a source/scene image from OBS and save it locally (proof shot).

    OBS writes the file itself at imageFilePath (required by the protocol), so
    against a REMOTE host the file lands on the OBS machine at that path — the
    report says so instead of pretending it is local.
    """
    path = _user_path(out) if out else Path.cwd() / f"{re.sub(r'[^A-Za-z0-9_-]+', '_', source)}.{fmt or 'png'}"
    image_format = (fmt or path.suffix.lstrip(".") or "png").lower()
    if image_format == "jpeg":
        image_format = "jpg"
    if image_format not in ("png", "jpg", "webp", "bmp"):
        fail(f"unsupported format {image_format!r} — png, jpg, webp, bmp", code=2)
    path.parent.mkdir(parents=True, exist_ok=True)

    payload: dict = {
        "sourceName": source,
        "imageFormat": image_format,
        "imageFilePath": str(path),
    }
    if scene:
        payload["sceneName"] = scene
    request("SaveSourceScreenshot", payload)

    local = path.exists()
    size = path.stat().st_size if local else 0
    if not local:
        # OBS accepted the request but the file is not on THIS machine —
        # we are talking to a remote OBS host; the file is there, not here.
        notice(f"OBS saved the screenshot on its own machine at {path}")
    if as_json:
        emit(
            {"saved": str(path), "exists_locally": local, "bytes": size, "source": source},
            as_json=True,
        )
        return
    if local:
        ok(f"saved {path} ({size} bytes)")
    else:
        ok(f"request accepted — file lives on the OBS host at {path}")


# ── events ───────────────────────────────────────────────────────────────────
@obs_app.command("events")
def events(
    topics: Annotated[
        str,
        typer.Option(
            "--topics",
            help=f"Comma-separated event groups (default all): {', '.join(sorted(EVENT_TOPICS))}.",
        ),
    ] = "",
    timeout: Annotated[
        float, typer.Option("--timeout", help="Stop after N seconds (0 = run until Ctrl+C).")
    ] = 0.0,
    as_json: Annotated[bool, typer.Option("--json", help="One JSON object per line (JSONL).")] = False,
) -> None:
    """Stream OBS events live (scene changes, output state, media, …).

    Runs until Ctrl+C or --timeout. Exit 0 on a clean stop; exit 1 if the
    connection drops mid-stream. Machine consumers: --json emits JSONL.
    """
    import time as _time

    from navig_obs.engine.client import EVENT_ALL, ObsClient

    mask = EVENT_ALL
    if topics.strip():
        mask = 0
        for name in [t.strip().lower() for t in topics.split(",") if t.strip()]:
            if name not in EVENT_TOPICS:
                fail(f"unknown topic {name!r} — choose from: {', '.join(sorted(EVENT_TOPICS))}", code=2)
            mask |= EVENT_TOPICS[name]

    t = target()
    try:
        client = ObsClient(t.host, t.port, t.password)
        client.connect(event_subscriptions=mask)
    except ObsAuthError as exc:
        fail(str(exc))
    except ObsConnectionError as exc:
        fail(f"{exc} — is OBS running with the WebSocket server enabled? (`navig obs doctor`)")

    started = _time.monotonic()
    deadline = (started + timeout) if timeout else None
    count = 0
    try:
        while True:
            event = client.stream_events(deadline=deadline)
            if event is None:
                break
            count += 1
            if as_json:
                # Strict JSONL: one compact object per line — typer.echo never
                # wraps or pretty-prints (rich print_json would break parsers).
                typer.echo(_json.dumps(event, default=str))
            else:
                console.print(
                    f"[cyan]●[/cyan] {event.get('eventType')} "
                    f"[dim]{_json.dumps(event.get('eventData') or {}, default=str)}[/dim]"
                )
    except KeyboardInterrupt:
        pass  # Ctrl+C is the normal way to stop the stream — exit 0 below.
    except ObsConnectionError as exc:
        client.close()
        fail(f"event stream lost after {count} event(s): {exc}")
    client.close()
    if not as_json:
        notice(f"event stream ended ({count} event(s))")


# ── request (raw passthrough) ────────────────────────────────────────────────
@obs_app.command("request")
def raw_request(
    request_type: Annotated[str, typer.Argument(help="obs-websocket v5 request type, e.g. GetVersion.")],
    data: Annotated[str, typer.Option("--data", help="requestData as a JSON object.")] = "",
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Send any obs-websocket v5 request and print its responseData.

    The escape hatch for what no verb covers yet. `navig obs request GetVersion`
    lists every valid name under availableRequests.
    """
    payload = None
    if data:
        try:
            payload = _json.loads(data)
        except _json.JSONDecodeError as exc:
            fail(f"--data must be valid JSON: {exc}", code=2)
        if not isinstance(payload, dict):
            fail("--data must be a JSON object", code=2)
    result = request(request_type, payload)
    if as_json or result:
        emit(result, as_json=True)
    else:
        ok(f"{request_type} ok")


# ── resource sub-apps ────────────────────────────────────────────────────────
from navig_obs.commands.media import (  # noqa: E402
    collections_app,
    hotkeys_app,
    media_app,
    profiles_app,
    transitions_app,
)
from navig_obs.commands.output import (  # noqa: E402
    record_app,
    replay_app,
    stream_app,
    studio_app,
    vcam_app,
)
from navig_obs.commands.scenes import items_app, scenes_app  # noqa: E402
from navig_obs.commands.clips import clips  # noqa: E402
from navig_obs.commands.session import marker, session_app  # noqa: E402
from navig_obs.commands.show import show_app  # noqa: E402
from navig_obs.commands.sources import audio_app, filters_app, sources_app  # noqa: E402

obs_app.add_typer(scenes_app)
obs_app.add_typer(items_app)
obs_app.add_typer(sources_app)
obs_app.add_typer(filters_app)
obs_app.add_typer(audio_app)
obs_app.add_typer(transitions_app)
obs_app.add_typer(stream_app)
obs_app.add_typer(record_app)
obs_app.add_typer(replay_app)
obs_app.add_typer(vcam_app)
obs_app.add_typer(studio_app)
obs_app.add_typer(collections_app)
obs_app.add_typer(profiles_app)
obs_app.add_typer(media_app)
obs_app.add_typer(hotkeys_app)
obs_app.add_typer(session_app)
obs_app.add_typer(show_app)
obs_app.command("marker")(marker)
obs_app.command("clips")(clips)
