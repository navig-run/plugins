"""`navig obs sources …`, `navig obs filters …`, `navig obs audio …` — inputs, their filters, their audio."""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

from navig_obs.commands._common import console, emit, fail, grid, ok, request

sources_app = typer.Typer(
    name="sources",
    help="Sources (inputs): list, kinds, add, remove, settings, active, refresh.",
    no_args_is_help=True,
)
audio_app = typer.Typer(
    name="audio", help="Source audio: monitor (who hears an input).", no_args_is_help=True
)
filters_app = typer.Typer(
    name="filters", help="Source filters: list, add, remove, settings, enable/disable.", no_args_is_help=True
)


def _parse_settings(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"--settings must be valid JSON: {exc}", code=2)
    if not isinstance(parsed, dict):
        fail("--settings must be a JSON object", code=2)
    return parsed


# ── sources ──────────────────────────────────────────────────────────────────
@sources_app.command("list")
def sources_list(
    kind: Annotated[str | None, typer.Option("--kind", help="Filter by input kind (e.g. image_source).")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Every input (source) known to OBS, with kind and uuid."""
    payload = {"inputKind": kind} if kind else None
    data = request("GetInputList", payload)

    def _render(d: dict) -> None:
        rows = [
            [str(i.get("inputName", "?")), str(i.get("inputKind", "?")), str(i.get("inputUuid", ""))[:8]]
            for i in d.get("inputs") or []
        ]
        grid("sources", [("name", True), ("kind", False), ("uuid", True)], rows, empty="no sources")

    emit(data, _render, as_json=as_json)


@sources_app.command("kinds")
def source_kinds(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """All input kinds this OBS build supports (use with `sources add <kind>`)."""
    data = request("GetInputKindList")
    kinds = data.get("inputKinds") or []
    if as_json:
        emit(data, as_json=True)
        return
    for k in kinds:
        console.print(f"  {k}")
    console.print(f"[dim]{len(kinds)} kind(s)[/dim]")


@sources_app.command("add")
def source_add(
    name: Annotated[str, typer.Argument(help="New source name.")],
    kind: Annotated[str, typer.Argument(help="Input kind (see `sources kinds`).")],
    settings: Annotated[str, typer.Option("--settings", help="Initial settings JSON.")] = "",
    scene: Annotated[str | None, typer.Option("--scene", help="Also place it into this scene.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Create a source; with --scene also add it to that scene."""
    payload: dict[str, Any] = {"inputName": name, "inputKind": kind}
    if settings:
        payload["inputSettings"] = _parse_settings(settings)
    if scene:
        payload["sceneName"] = scene
    data = request("CreateInput", payload)
    emit(data, as_json=as_json)
    if not as_json:
        ok(f"source {name!r} ({kind}) created" + (f" in scene {scene!r}" if scene else ""))


@sources_app.command("remove")
def source_remove(
    name: Annotated[str, typer.Argument(help="Source (input) name.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Delete a source everywhere it is used (asks first)."""
    if not yes and not typer.confirm(
        f"Delete source {name!r} and its scene items (not undoable)?", default=False
    ):
        console.print("Cancelled.")
        raise typer.Exit(0)
    request("RemoveInput", {"inputName": name})
    ok(f"source {name!r} removed")


@sources_app.command("settings")
def source_settings(
    name: Annotated[str, typer.Argument(help="Source (input) name.")],
    set_: Annotated[str, typer.Option("--set", help='Settings JSON to apply, e.g. \'{"file":"C:\\\\img.png"}\'.')] = "",
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Read (default) or write a source's settings — e.g. repoint an image file."""
    if set_:
        emit(request("SetInputSettings", {"inputName": name, "inputSettings": _parse_settings(set_)}), as_json=as_json)
        if not as_json:
            ok(f"source {name!r} settings updated")
        return
    emit(request("GetInputSettings", {"inputName": name}), as_json=as_json)


@sources_app.command("active")
def source_active(
    name: Annotated[str, typer.Argument(help="Source (input) name.")],
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Whether the source is currently active (showing) in the program output."""
    emit(request("GetInputActive", {"inputName": name}), as_json=as_json)


@sources_app.command("defaults")
def source_defaults(
    name: Annotated[str, typer.Argument(help="Source (input) name.")],
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """A source's default settings (factory values for its kind)."""
    emit(request("GetInputDefaultSettings", {"inputName": name}), as_json=as_json)


@sources_app.command("refresh")
def source_refresh(
    name: Annotated[str, typer.Argument(help="Browser source name.")],
) -> None:
    """Reload a browser source without its cache (its "Refresh cache of current page")."""
    request("PressInputPropertiesButton", {"inputName": name, "propertyName": "refreshnocache"})
    ok(f"source {name!r} reloaded (no cache)")


# ── audio ────────────────────────────────────────────────────────────────────
MONITOR_TYPES = {
    "none": "OBS_MONITORING_TYPE_NONE",
    "monitor": "OBS_MONITORING_TYPE_MONITOR_ONLY",
    "both": "OBS_MONITORING_TYPE_MONITOR_AND_OUTPUT",
}
_MONITOR_NAMES = {v: k for k, v in MONITOR_TYPES.items()}


@audio_app.command("monitor")
def audio_monitor(
    name: Annotated[str, typer.Argument(help="Source (input) name.")],
    type_: Annotated[
        str | None,
        typer.Option(
            "--type",
            help="none | monitor (operator only) | both (operator + stream). Omit to read.",
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Read (default) or set who hears a source: nobody extra, the operator, or both."""
    if type_ is None:
        data = request("GetInputAudioMonitorType", {"inputName": name})
        raw = str(data.get("monitorType") or "")
        emit(
            {"inputName": name, "monitorType": raw, "type": _MONITOR_NAMES.get(raw)},
            lambda d: console.print(f"{name}: {d['type'] or d['monitorType']}  [dim]{d['monitorType']}[/dim]"),
            as_json=as_json,
        )
        return
    key = type_.strip().lower()
    if key not in MONITOR_TYPES:
        fail(f"--type must be one of: {', '.join(MONITOR_TYPES)}", code=2)
    request("SetInputAudioMonitorType", {"inputName": name, "monitorType": MONITOR_TYPES[key]})
    if as_json:
        emit({"inputName": name, "monitorType": MONITOR_TYPES[key], "type": key}, as_json=True)
        return
    ok(f"source {name!r} monitor → {key}")


# ── filters ──────────────────────────────────────────────────────────────────
@filters_app.command("list")
def filters_list(
    source: Annotated[str, typer.Option("--source", help="Source (input) name.")],
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Filters on a source (audio/video effects, in order)."""
    data = request("GetSourceFilterList", {"sourceName": source})

    def _render(d: dict) -> None:
        rows = [
            [
                str(f.get("filterName", "?")),
                str(f.get("filterKind", "?")),
                "[green]on[/green]" if f.get("filterEnabled") else "[dim]off[/dim]",
            ]
            for f in d.get("filters") or []
        ]
        grid(f"filters · {source}", [("filter", True), ("kind", False), ("state", True)], rows, empty="no filters")

    emit(data, _render, as_json=as_json)


@filters_app.command("add")
def filter_add(
    source: Annotated[str, typer.Option("--source", help="Source (input) name.")],
    name: Annotated[str, typer.Argument(help="New filter name.")],
    kind: Annotated[str, typer.Argument(help="Filter kind (e.g. compress_filter, scale_filter).")],
    settings: Annotated[str, typer.Option("--settings", help="Filter settings JSON.")] = "",
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Add a filter to a source."""
    payload: dict[str, Any] = {"sourceName": source, "filterName": name, "filterKind": kind}
    if settings:
        payload["filterSettings"] = _parse_settings(settings)
    emit(request("CreateSourceFilter", payload), as_json=as_json)
    if not as_json:
        ok(f"filter {name!r} ({kind}) added to {source!r}")


@filters_app.command("remove")
def filter_remove(
    source: Annotated[str, typer.Option("--source", help="Source (input) name.")],
    name: Annotated[str, typer.Argument(help="Filter name to remove.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Remove a filter from a source."""
    if not yes and not typer.confirm(f"Remove filter {name!r} from {source!r}?", default=False):
        console.print("Cancelled.")
        raise typer.Exit(0)
    request("RemoveSourceFilter", {"sourceName": source, "filterName": name})
    ok(f"filter {name!r} removed from {source!r}")


@filters_app.command("settings")
def filter_settings(
    source: Annotated[str, typer.Option("--source", help="Source (input) name.")],
    name: Annotated[str, typer.Argument(help="Filter name.")],
    set_: Annotated[str, typer.Option("--set", help="Settings JSON to apply.")] = "",
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Read (default) or write a filter's settings."""
    if set_:
        emit(
            request(
                "SetSourceFilterSettings",
                {"sourceName": source, "filterName": name, "filterSettings": _parse_settings(set_)},
            ),
            as_json=as_json,
        )
        if not as_json:
            ok(f"filter {name!r} settings updated")
        return
    emit(request("GetSourceFilterSettings", {"sourceName": source, "filterName": name}), as_json=as_json)


@filters_app.command("enable")
def filter_enable(
    source: Annotated[str, typer.Option("--source", help="Source (input) name.")],
    name: Annotated[str, typer.Argument(help="Filter name.")],
    enabled: Annotated[bool, typer.Option("--on/--off", help="Enable or disable the filter.")] = True,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON.")] = False,
) -> None:
    """Enable or disable one filter."""
    emit(
        request(
            "SetSourceFilterEnabled",
            {"sourceName": source, "filterName": name, "filterEnabled": enabled},
        ),
        as_json=as_json,
    )
    if not as_json:
        ok(f"filter {name!r} → {'on' if enabled else 'off'}")
