"""`navig obs` groups: collections · profiles · transitions · media · hotkeys."""

from __future__ import annotations

from typing import Annotated

import typer

from navig_obs.commands._common import console, emit, grid, kv_table, ok, request

collections_app = typer.Typer(
    name="collections", help="Scene collections: list, current, create, use.", no_args_is_help=True
)
profiles_app = typer.Typer(name="profiles", help="Profiles: list, current, use.", no_args_is_help=True)
transitions_app = typer.Typer(
    name="transitions", help="Scene transitions: list, current, switch, settings.", no_args_is_help=True
)
media_app = typer.Typer(
    name="media", help="Media sources: play, pause, restart, stop, status.", no_args_is_help=True
)
hotkeys_app = typer.Typer(name="hotkeys", help="Hotkeys: list, send.", no_args_is_help=True)


# ── scene collections ────────────────────────────────────────────────────────
@collections_app.command("list")
def collections_list(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """All scene collections; mark the current one."""
    data = request("GetSceneCollectionList")

    def _render(d: dict) -> None:
        current = d.get("currentSceneCollectionName")
        for name in d.get("sceneCollections") or []:
            if name == current:
                console.print(f"  [green]●[/green] {name} [cyan](current)[/cyan]")
            else:
                console.print(f"  [dim]○[/dim] {name}")

    emit(data, _render, as_json=as_json)


@collections_app.command("current")
def collection_current(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """The active scene collection."""
    emit(request("GetCurrentSceneCollection"), as_json=as_json)


@collections_app.command("create")
def collection_create(
    name: Annotated[str, typer.Argument(help="New collection name.")],
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Create AND switch to a new scene collection (OBS switches on create)."""
    emit(request("CreateSceneCollection", {"sceneCollectionName": name}), as_json=as_json)
    if not as_json:
        ok(f"collection {name!r} created and activated")


@collections_app.command("use")
def collection_use(
    name: Annotated[str, typer.Argument(help="Collection to switch to.")],
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Switch to another scene collection."""
    emit(request("SetCurrentSceneCollection", {"sceneCollectionName": name}), as_json=as_json)
    if not as_json:
        ok(f"collection → {name!r}")


# ── profiles ─────────────────────────────────────────────────────────────────
@profiles_app.command("list")
def profiles_list(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """All profiles; mark the current one."""
    data = request("GetProfileList")

    def _render(d: dict) -> None:
        current = d.get("currentProfileName")
        for name in d.get("profiles") or []:
            if name == current:
                console.print(f"  [green]●[/green] {name} [cyan](current)[/cyan]")
            else:
                console.print(f"  [dim]○[/dim] {name}")

    emit(data, _render, as_json=as_json)


@profiles_app.command("current")
def profile_current(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """The active profile."""
    emit(request("GetCurrentProfile"), as_json=as_json)


@profiles_app.command("use")
def profile_use(
    name: Annotated[str, typer.Argument(help="Profile to switch to.")],
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Switch to another profile."""
    emit(request("SetCurrentProfile", {"profileName": name}), as_json=as_json)
    if not as_json:
        ok(f"profile → {name!r}")


# ── transitions ──────────────────────────────────────────────────────────────
@transitions_app.command("list")
def transitions_list(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Available scene transitions; mark the current one."""
    data = request("GetTransitionList")

    def _render(d: dict) -> None:
        current = d.get("currentSceneTransitionName")
        rows = [
            [
                str(t.get("transitionName", "?")),
                str(t.get("transitionKind", "?")),
                "[green]current[/green]" if t.get("transitionName") == current else "",
            ]
            for t in d.get("transitions") or []
        ]
        grid("transitions", [("transition", True), ("kind", False), ("state", True)], rows, empty="no transitions")

    emit(data, _render, as_json=as_json)


@transitions_app.command("current")
def transition_current(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """The active scene transition and its settings."""
    emit(request("GetCurrentSceneTransition"), as_json=as_json)


@transitions_app.command("set")
def transition_set(
    name: Annotated[str, typer.Argument(help="Transition name (see `transitions list`).")],
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Switch the active scene transition."""
    emit(request("SetCurrentSceneTransition", {"transitionName": name}), as_json=as_json)
    if not as_json:
        ok(f"transition → {name!r}")


@transitions_app.command("settings")
def transition_settings(
    settings: Annotated[str, typer.Option("--set", help='Transition settings JSON, e.g. \'{"duration":300}\'.')] = "",
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Set the current transition's settings (e.g. duration)."""
    from navig_obs.commands.sources import _parse_settings

    if settings:
        emit(
            request(
                "SetSceneTransitionSettings",
                {"transitionSettings": _parse_settings(settings)},
            ),
            as_json=as_json,
        )
        if not as_json:
            ok("transition settings applied")
        return
    emit(request("GetCurrentSceneTransition"), as_json=as_json)


# ── media ────────────────────────────────────────────────────────────────────
@media_app.command("status")
def media_status(
    source: Annotated[str, typer.Option("--source", help="Media source (input) name.")],
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Media source state: playing/paused/stopped, duration, cursor."""
    data = request("GetMediaInputStatus", {"inputName": source})

    def _render(d: dict) -> None:
        state = str(d.get("mediaState", "?"))
        colour = {"OBS_MEDIA_STATE_PLAYING": "green", "OBS_MEDIA_STATE_PAUSED": "yellow"}.get(state, "dim")
        dur = int(d.get("mediaDuration") or 0)
        cur = int(d.get("mediaCursor") or 0)

        def _t(ms: int) -> str:
            return f"{ms // 60000:02d}:{ms % 60000 // 1000:02d}"

        console.print(
            kv_table(
                f"media · {source}",
                [
                    ("state", f"[{colour}]{state}[/{colour}]"),
                    ("cursor", f"{_t(cur)} / {_t(dur)}" if dur else "—"),
                ],
            )
        )

    emit(data, _render, as_json=as_json)


# obs-websocket v5 has ONE request for every media transport action:
# TriggerMediaInputAction + an `mediaAction` enum. The per-action names this
# module used to send (PlayMediaInput, PauseMediaInput, RestartMediaInput,
# StopMediaInput) do not exist in v5 — OBS answers them with 204
# InvalidRequestType, so every media verb failed against a real OBS while the
# fake server, which answers whatever it is scripted with, never noticed.
MEDIA_ACTION = {
    "play": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY",
    "pause": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PAUSE",
    "restart": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART",
    "stop": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_STOP",
}


def _media_action(source: str, action: str, done: str, *, as_json: bool) -> None:
    emit(
        request(
            "TriggerMediaInputAction",
            {"inputName": source, "mediaAction": MEDIA_ACTION[action]},
        ),
        as_json=as_json,
    )
    if not as_json:
        ok(f"{source}: {done}")


@media_app.command("play")
def media_play(
    source: Annotated[str, typer.Option("--source", help="Media source (input) name.")],
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Play a media source (a finished one plays again from the start)."""
    _media_action(source, "play", "play", as_json=as_json)


@media_app.command("pause")
def media_pause(
    source: Annotated[str, typer.Option("--source", help="Media source (input) name.")],
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Pause a media source."""
    _media_action(source, "pause", "paused", as_json=as_json)


@media_app.command("restart")
def media_restart(
    source: Annotated[str, typer.Option("--source", help="Media source (input) name.")],
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Restart a media source from the beginning."""
    _media_action(source, "restart", "restarted", as_json=as_json)


@media_app.command("stop")
def media_stop(
    source: Annotated[str, typer.Option("--source", help="Media source (input) name.")],
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Stop a media source."""
    _media_action(source, "stop", "stopped", as_json=as_json)


# ── hotkeys ──────────────────────────────────────────────────────────────────
@hotkeys_app.command("list")
def hotkeys_list(
    prefix: Annotated[str, typer.Option("--prefix", help="Only hotkeys starting with this prefix.")] = "",
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Every hotkey OBS knows (names usable with `hotkeys send`)."""
    data = request("GetHotkeyList")
    names = [h for h in (data.get("hotkeys") or []) if not prefix or h.startswith(prefix)]
    if as_json:
        emit({"hotkeys": names}, as_json=True)
        return
    for name in names:
        console.print(f"  {name}")
    console.print(f"[dim]{len(names)} hotkey(s)[/dim]")


@hotkeys_app.command("send")
def hotkeys_send(
    name: Annotated[str, typer.Argument(help="Hotkey name (see `hotkeys list`).")],
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Trigger a hotkey by name (same effect as pressing it in OBS)."""
    emit(request("TriggerHotkeyByName", {"hotkeyName": name}), as_json=as_json)
    if not as_json:
        ok(f"hotkey triggered: {name}")
