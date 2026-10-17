"""`navig obs scenes …` and `navig obs items …` — scene + scene-item control."""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

from navig_obs.commands._common import console, emit, fail, grid, ok, request

scenes_app = typer.Typer(
    name="scenes", help="Scenes: list, create, remove, activate, current.", no_args_is_help=True
)
items_app = typer.Typer(
    name="items", help="Scene items: list, visibility, transform, remove, duplicate.", no_args_is_help=True
)


def _parse_json_option(raw: str, what: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"{what} must be valid JSON: {exc}", code=2)
    if not isinstance(parsed, dict):
        fail(f"{what} must be a JSON object", code=2)
    return parsed


def _scene_or_current(scene: str | None) -> str:
    """Resolve --scene, falling back to the current program scene."""
    if scene:
        return scene
    data = request("GetCurrentProgramScene")
    name = data.get("currentProgramSceneName")
    if not name:
        fail("no --scene given and OBS reported no current scene")
    return str(name)


# ── scenes ───────────────────────────────────────────────────────────────────
@scenes_app.command("list")
def scenes_list(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """All scenes; mark the current program/preview scene."""
    data = request("GetSceneList")
    program = data.get("currentProgramSceneName")
    preview = data.get("currentPreviewSceneName")

    def _render(d: dict) -> None:
        rows = []
        for s in d.get("scenes") or []:
            name = s.get("sceneName", "?")
            marks = []
            if name == program:
                marks.append("[green]program[/green]")
            if name == preview:
                marks.append("[cyan]preview[/cyan]")
            rows.append([name, ", ".join(marks) or ""])
        grid(
            "scenes",
            [("scene", True), ("state", False)],
            rows,
            empty="no scenes",
        )

    emit(data, _render, as_json=as_json)


@scenes_app.command("current")
def scene_current(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """The current program scene."""
    emit(request("GetCurrentProgramScene"), as_json=as_json)


@scenes_app.command("create")
def scene_create(
    name: Annotated[str, typer.Argument(help="New scene name.")],
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Create a scene."""
    emit(request("CreateScene", {"sceneName": name}), as_json=as_json)
    if not as_json:
        ok(f"scene {name!r} created")


@scenes_app.command("remove")
def scene_remove(
    name: Annotated[str, typer.Argument(help="Scene to delete.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Delete a scene (asks first; OBS scene deletion is not undoable)."""
    if not yes and not typer.confirm(f"Delete scene {name!r} (not undoable)?", default=False):
        console.print("Cancelled.")
        raise typer.Exit(0)
    request("RemoveScene", {"sceneName": name})
    ok(f"scene {name!r} removed")


@scenes_app.command("activate")
def scene_activate(
    name: Annotated[str, typer.Argument(help="Scene to switch the program output to.")],
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Switch the live program output to a scene."""
    emit(request("SetCurrentProgramScene", {"sceneName": name}), as_json=as_json)
    if not as_json:
        ok(f"program scene → {name!r}")


# ── items ────────────────────────────────────────────────────────────────────
@items_app.command("list")
def items_list(
    scene: Annotated[str | None, typer.Option("--scene", help="Scene (default: current program scene).")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Scene items (sources placed in a scene) with id, visibility and kind."""
    resolved = _scene_or_current(scene)
    data = request("GetSceneItemList", {"sceneName": resolved})

    def _render(d: dict) -> None:
        rows = []
        for it in d.get("sceneItems") or []:
            rows.append(
                [
                    str(it.get("sceneItemId", "?")),
                    str(it.get("inputName") or it.get("sourceName") or "?"),
                    str(it.get("inputKind") or ""),
                    "[green]visible[/green]" if it.get("sceneItemEnabled") else "[dim]hidden[/dim]",
                    "locked" if it.get("sceneItemLocked") else "",
                ]
            )
        grid(
            f"items · {resolved}",
            [("id", True), ("source", False), ("kind", True), ("state", True), ("lock", True)],
            rows,
            empty="no items in this scene",
        )

    emit(data, _render, as_json=as_json)


@items_app.command("visible")
def item_visible(
    item_id: Annotated[int, typer.Argument(help="Scene item id (see `items list`).")],
    visible: Annotated[bool, typer.Option("--on/--off", help="Show or hide the item.")] = True,
    scene: Annotated[str | None, typer.Option("--scene", help="Scene (default: current program scene).")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Show or hide a scene item."""
    resolved = _scene_or_current(scene)
    emit(
        request(
            "SetSceneItemEnabled",
            {"sceneName": resolved, "sceneItemId": item_id, "sceneItemEnabled": visible},
        ),
        as_json=as_json,
    )
    if not as_json:
        ok(f"item {item_id} in {resolved!r} → {'visible' if visible else 'hidden'}")


@items_app.command("transform")
def item_transform(
    item_id: Annotated[int, typer.Argument(help="Scene item id (see `items list`).")],
    settings: Annotated[
        str, typer.Option("--settings", help='Transform JSON, e.g. \'{"positionX":100,"scaleX":0.5}\'.')
    ] = "",
    scene: Annotated[str | None, typer.Option("--scene", help="Scene (default: current program scene).")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Set a scene item's transform (position/size/rotation/crop).

    Without --settings prints the current transform.
    """
    resolved = _scene_or_current(scene)
    if not settings:
        data = request("GetSceneItemTransform", {"sceneName": resolved, "sceneItemId": item_id})
        emit(data, as_json=as_json)
        return
    transform = _parse_json_option(settings, "--settings")
    emit(
        request(
            "SetSceneItemTransform",
            {"sceneName": resolved, "sceneItemId": item_id, "sceneItemTransform": transform},
        ),
        as_json=as_json,
    )
    if not as_json:
        ok(f"item {item_id} transform updated")


@items_app.command("remove")
def item_remove(
    item_id: Annotated[int, typer.Argument(help="Scene item id (see `items list`).")],
    scene: Annotated[str | None, typer.Option("--scene", help="Scene (default: current program scene).")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Remove a scene item from its scene (the source itself stays)."""
    resolved = _scene_or_current(scene)
    if not yes and not typer.confirm(
        f"Remove item {item_id} from scene {resolved!r} (source stays)?", default=False
    ):
        console.print("Cancelled.")
        raise typer.Exit(0)
    request("RemoveSceneItem", {"sceneName": resolved, "sceneItemId": item_id})
    ok(f"item {item_id} removed from {resolved!r}")


@items_app.command("duplicate")
def item_duplicate(
    item_id: Annotated[int, typer.Argument(help="Scene item id to duplicate.")],
    scene: Annotated[str | None, typer.Option("--scene", help="Scene (default: current program scene).")] = None,
    dest_scene: Annotated[
        str | None, typer.Option("--dest-scene", help="Target scene (default: same scene).")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Duplicate a scene item (optionally into another scene)."""
    resolved = _scene_or_current(scene)
    payload: dict[str, Any] = {"sceneName": resolved, "sceneItemId": item_id}
    if dest_scene:
        payload["destSceneName"] = dest_scene
    data = request("DuplicateSceneItem", payload)
    emit(data, as_json=as_json)
    if not as_json:
        ok(f"duplicated item {item_id} → {dest_scene or resolved!r}")
