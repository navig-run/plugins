"""`navig obs show` — the show-events bridge by hand (setup, test, inspect).

The same moves the gateway service makes (``navig_obs.show``): a cue through
the one cue input, a toast through the overlay. Nothing here goes live — a
toast or a cue only reaches viewers if the operator is already streaming.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Annotated, Any

import typer

from navig_obs.commands._common import (
    ObsErrorAll,
    console,
    emit,
    fail,
    kv_table,
    ok,
    target,
)
from navig_obs.engine.client import ObsClient
from navig_obs.engine.errors import ObsRequestError
from navig_obs.show import actions, civic, feed, voices
from navig_obs.show import say as say_mod
from navig_obs.show.cues import CueBook, CueBookError
from navig_obs.show.settings import ShowSettings, load_settings, state_dir

show_app = typer.Typer(
    name="show",
    help="Show events: deploy toasts, store-sale cash ring, the Grid Pigeon's cues.",
    no_args_is_help=True,
)


# obs-websocket 207 = "not ready": OBS is loading a collection or a profile.
# Switching collection mid-run used to abort the whole command on that answer.
NOT_READY = 207
NOT_READY_TRIES = 6
NOT_READY_WAIT_S = 1.5


def _obs_once(move: Any) -> Any:
    """Run ``move`` on a fresh session, waiting out a collection/profile load."""
    import time

    t = target()
    for attempt in range(NOT_READY_TRIES):
        try:
            with ObsClient(t.host, t.port, t.password) as client:
                return move(client)
        except ObsRequestError as exc:
            if exc.code != NOT_READY or attempt == NOT_READY_TRIES - 1:
                raise
            time.sleep(NOT_READY_WAIT_S)
    return None


def _with_obs(move: Any) -> Any:
    try:
        return _obs_once(move)
    except ObsErrorAll as exc:
        fail(f"{exc}")
    return None


def _book() -> CueBook:
    try:
        return CueBook.load(load_settings().cues_file)
    except CueBookError as exc:
        fail(str(exc))
    return CueBook()


@show_app.command("status")
def show_status(
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable JSON.")
    ] = False,
) -> None:
    """Settings, the cue book and the last feed position (no OBS needed)."""
    s = load_settings()
    try:
        cues = sorted(CueBook.load(s.cues_file).cues)
        book_error = None
    except CueBookError as exc:
        cues, book_error = [], str(exc)
    st = feed.load_state(state_dir() / "feed-state.json")
    civic_state = civic.load_state(state_dir() / "civic-state.json")
    data = {
        "enabled": s.enabled,
        "feed": s.patch_feed or None,
        "pollSeconds": s.poll_seconds,
        "cuesFile": str(s.cues_file),
        "cues": cues,
        "cueBookError": book_error,
        "cueInput": s.cue_input,
        "toastEvent": s.toast_event,
        "feedState": st.to_json() if st else None,
        "civicFeed": s.civic_feed or None,
        "civicEvent": s.civic_event,
        "civicState": civic_state.to_json() if civic_state else None,
    }

    def _render(d: dict) -> None:
        console.print(
            kv_table(
                "obs show",
                [
                    (
                        "enabled",
                        "[green]on[/green]"
                        if d["enabled"]
                        else "[dim]off[/dim] (obs.show.enabled)",
                    ),
                    ("feed", d["feed"] or "[dim]off[/dim]"),
                    ("cues", ", ".join(d["cues"]) or f"[red]{d['cueBookError']}[/red]"),
                    ("cue input", d["cueInput"]),
                    ("last patch", str((d["feedState"] or {}).get("seq") or "—")),
                    ("civic feed", d["civicFeed"] or "[dim]off[/dim]"),
                    ("last civic", str((d["civicState"] or {}).get("since") or "—")),
                ],
            )
        )

    emit(data, _render, as_json=as_json)


@show_app.command("setup")
def show_setup(
    scene: Annotated[
        list[str],
        typer.Option("--scene", help="Scene to carry the cue input (repeatable)."),
    ] = [],  # noqa: B006
    all_scenes: Annotated[
        bool,
        typer.Option("--all-scenes", help="Every scene of the current collection."),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable JSON.")
    ] = False,
) -> None:
    """Create the one cue input and place it in the scenes that go on air."""
    s = load_settings()

    def move(client: ObsClient) -> dict:
        scenes = list(scene)
        if all_scenes:
            scenes = [
                x.get("sceneName")
                for x in (client.request("GetSceneList").get("scenes") or [])
            ]
        if not scenes:
            fail("name a --scene or pass --all-scenes", code=2)
        return actions.ensure_cue_input(client, s.cue_input, [x for x in scenes if x])

    result = _with_obs(move)
    emit(
        result,
        lambda d: ok(
            f"{s.cue_input}: created in {d['created'] or '—'}, placed in {d['placed'] or '—'}"
        ),
        as_json=as_json,
    )


@show_app.command("cue")
def show_cue(
    cue_id: Annotated[
        str, typer.Argument(help="Cue id from the cue book, e.g. sale-ring.")
    ],
) -> None:
    """Play one cue now (no cooldown) through the cue input."""
    cue = _book().get(cue_id)
    if cue is None:
        fail(f"no cue {cue_id!r} in the cue book", code=2)
    s = load_settings()
    _with_obs(lambda c: actions.play_cue(c, s.cue_input, cue))
    ok(f"{cue_id} → {s.cue_input}")


@show_app.command("toast")
def show_toast(
    kind: Annotated[str, typer.Option("--kind", help="patch | compiling")] = "patch",
    seq: Annotated[int, typer.Option("--seq", help="Patch number (kind=patch).")] = 0,
    headline: Annotated[
        str, typer.Option("--headline", help="World-voiced headline (kind=patch).")
    ] = "",
    count: Annotated[int, typer.Option("--count", help="Changes in the patch.")] = 0,
) -> None:
    """Show a toast on the overlay (what a deploy would show)."""
    if kind not in ("patch", "compiling"):
        fail("--kind must be patch or compiling", code=2)
    toast: dict[str, Any] = {"kind": kind, "count": count}
    if kind == "patch":
        toast.update({"seq": seq, "headline": headline})
    s = load_settings()
    _with_obs(lambda c: actions.emit_toast(c, s.toast_event, toast))
    ok(f"toast {kind} → {s.toast_event}")


@show_app.command("poll")
def show_poll(
    fire: Annotated[
        bool, typer.Option("--fire", help="Show the toasts and remember the position.")
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable JSON.")
    ] = False,
) -> None:
    """Read the patch feed once: what WOULD show (or, with --fire, show it)."""
    s = load_settings()
    if not s.patch_feed:
        fail("obs.show.patch_feed is empty — the deploy watcher is off", code=2)
    path = state_dir() / "feed-state.json"
    try:
        doc = feed.fetch(s.patch_feed)
    except Exception as exc:  # noqa: BLE001 — network/JSON failure → honest exit
        fail(f"feed unreachable ({exc})")
    toasts, state = feed.diff(feed.load_state(path), doc)
    if fire:
        feed.save_state(path, state)
        for t in toasts:
            _with_obs(lambda c, t=t: actions.emit_toast(c, s.toast_event, t))
    emit(
        {"toasts": toasts, "state": state.to_json(), "fired": fire},
        lambda d: console.print(
            f"{len(d['toasts'])} toast(s) · feed at patch {d['state']['seq']}"
        ),
        as_json=as_json,
    )


PIGEON_MODES = ("observer", "chaos", "courier")
# The moments of the overlay's line book (design/broadcast/obs/pigeon_lines.json
# in Schema). Listed for --help; any well-formed name is sent, so a group
# added there works before this list learns it.
PIGEON_MOMENTS = (
    "clock", "hydrate", "posture", "stretch", "eyes", "live_start", "live_end",
    "recording", "uptime_30m", "uptime_1h", "uptime_2h", "uptime_long", "fury",
    "rageclick", "idle", "back", "lag", "resume", "late", "scene", "musing",
)
_MOMENT = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


@show_app.command("pigeon")
def show_pigeon(
    act: Annotated[
        str,
        typer.Argument(
            help="A beat: enter, exit, join, passport, mission, message, warning, "
            "transmission, redeye, stare, screenpeck, malfunction, sleep, … "
            "(every beat is a button on the overlay's ?panel=1 test panel)."
        ),
    ] = "",
    mode: Annotated[
        str, typer.Option("--mode", help="observer | chaos | courier")
    ] = "",
    say: Annotated[
        str,
        typer.Option(
            "--say",
            help="One of his own lines, by moment: "
            + ", ".join(PIGEON_MOMENTS)
            + " (the overlay's pigeon_lines.json; a new group there works too).",
        ),
    ] = "",
    index: Annotated[
        int | None,
        typer.Option("--index", help="With --say: that line of the group (0-based)."),
    ] = None,
) -> None:
    """Cue the Grid Pigeon by hand: one beat, one of his lines, or his temper.

        navig obs show pigeon passport           a beat
        navig obs show pigeon --say hydrate      a line, in the stage language
        navig obs show pigeon --say clock --index 2
        navig obs show pigeon --mode chaos       his temper
    """
    if not act and not mode and not say:
        fail("name a beat, or pass --say or --mode", code=2)
    if act and say:
        fail("a beat or a line, not both", code=2)
    if mode and mode not in PIGEON_MODES:
        fail(f"--mode must be one of {', '.join(PIGEON_MODES)}", code=2)
    if say and not _MOMENT.match(say):
        fail("--say takes a moment name (letters, digits, _), e.g. hydrate", code=2)
    if index is not None and not say:
        fail("--index goes with --say", code=2)
    if index is not None and index < 0:
        fail("--index starts at 0", code=2)
    data: dict[str, Any] = {}
    if act:
        data["act"] = act
    if say:
        data["say"] = say
        if index is not None:
            data["index"] = index
    if mode:
        data["mode"] = mode
    s = load_settings()
    _with_obs(lambda c: actions.emit_event(c, s.pigeon_event, data))
    what = act or (f"says {say}" + (f"[{index}]" if index is not None else "")) or "·"
    ok(f"pigeon {what}{f' ({mode})' if mode else ''} → {s.pigeon_event}")


@show_app.command("civic")
def show_civic(
    fire: Annotated[
        bool, typer.Option("--fire", help="Send the events and remember the position.")
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable JSON.")
    ] = False,
) -> None:
    """Read the civic feed once: what the pigeon WOULD react to (--fire sends it)."""
    s = load_settings()
    if not s.civic_feed:
        fail("obs.show.civic_feed is empty — the civic watcher is off", code=2)
    path = state_dir() / "civic-state.json"
    prev = civic.load_state(path)
    try:
        doc = feed.fetch(civic.feed_url(s.civic_feed, prev))
    except Exception as exc:  # noqa: BLE001 — network/JSON failure → honest exit
        fail(f"civic feed unreachable ({exc})")
    events, state = civic.diff(prev, doc)
    if fire:
        civic.save_state(path, state)
        lang = _stage_lang() if (s.civic_say and events) else None
        for ev in events:
            _with_obs(lambda c, ev=ev: actions.emit_event(c, s.civic_event, ev))
            if s.civic_say:
                _say_civic(s, ev, lang)

    def _render(d: dict) -> None:
        head = (
            "first look: cursor seeded, nothing sent"
            if d["seeded"]
            else f"{len(d['events'])} event(s): {', '.join(d['events']) or '—'}"
        )
        console.print(f"{head} · cursor {d['state']['since'] or '—'}")

    emit(
        {
            "events": [e.get("kind") for e in events],
            "state": state.to_json(),
            "fired": fire,
            "seeded": prev is None,
        },
        _render,
        as_json=as_json,
    )


def _stage_lang() -> str | None:
    """The loaded collection's language (``SCHEMA BROADCAST (ru)`` → ru), or
    None when OBS is closed or the collection names none."""
    t = target()
    try:
        with ObsClient(t.host, t.port, t.password) as client:
            return voices.lang_of(actions.current_collection(client))
    except ObsErrorAll:
        return None


def _say_civic(s: ShowSettings, ev: dict[str, Any], lang: str | None) -> None:
    """A civic beat spoken (obs.show.civic_say): same path as `show say`."""
    code, _ = say_mod.voice_for(s, lang)
    text = civic.line(ev, code)
    if not text:
        return
    p = say_mod.prepare(
        say_mod.plan_for(
            s, text, lang=code, kicker=voices.KICKER.get(code, "THE GRID"),
            tone=civic.tone(ev),
        )
    )
    for w in p.warnings:
        console.print(f"[yellow]○[/yellow] {w}")
    _with_obs(
        lambda c: say_mod.perform(
            c,
            p,
            toast_event=s.toast_event,
            pigeon_event=s.pigeon_event,
            cue_input=s.cue_input,
        )
    )


TEMPLATES = ("deploy",)


@show_app.command("say")
def show_say(
    text: Annotated[
        str | None,
        typer.Argument(help="What the pigeon says (and the card shows). "
                       "Omit it with --template."),
    ] = None,
    title: Annotated[
        str | None, typer.Option("--title", help="Card title (default: TEXT).")
    ] = None,
    kicker: Annotated[
        str, typer.Option("--kicker", help="Small line above the title.")
    ] = "",
    tone: Annotated[
        str, typer.Option("--tone", help="deploy | game | live | passport | info")
    ] = "info",
    lang: Annotated[
        str | None,
        typer.Option(
            "--lang",
            help="en | fr | ru. Default: the loaded collection's language "
            "(SCHEMA BROADCAST (ru) → ru), else obs.show.say_lang.",
        ),
    ] = None,
    template: Annotated[
        str | None,
        typer.Option("--template", help="A world line in the stage's language: deploy."),
    ] = None,
    image: Annotated[
        str | None,
        typer.Option("--image", help="https URL of a picture for the card (e.g. a game's art)."),
    ] = None,
    provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            help="TTS provider for this line (default: the language's voice, "
            "obs.show.say_voices).",
        ),
    ] = None,
    voice: Annotated[
        str | None, typer.Option("--voice", help="Voice id/name for the provider.")
    ] = None,
    fresh: Annotated[
        bool, typer.Option("--fresh", help="Ignore a cached clip and synthesize again.")
    ] = False,
    no_voice: Annotated[
        bool, typer.Option("--no-voice", help="Card + pigeon only, no synthesis.")
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the plan; no synthesis, no OBS call."),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable JSON.")
    ] = False,
) -> None:
    """Speak a line on stream: alert card + the pigeon talks + the clip plays.

    The voice follows the stage: the loaded collection's language picks the
    voice and how names are said (SCHEMA → Skeema / schéma / Схема). The clip
    is cached and never re-synthesized (--fresh forces it). A failed synthesis
    still shows the card (warning, exit 0).

    \b
      navig obs show say "SCHEMA deployed." --tone deploy
      navig obs show say --template deploy
      navig obs show say "Bad Cheese is free" --tone game --image https://…/art.jpg
    """
    if tone not in say_mod.TONES:
        fail(f"--tone must be one of: {', '.join(say_mod.TONES)}", code=2)
    if lang is not None and not voices.norm_lang(lang):
        fail(f"--lang must be one of: {', '.join(voices.LANGS)}", code=2)
    if template is not None and template not in TEMPLATES:
        fail(f"--template must be one of: {', '.join(TEMPLATES)}", code=2)
    if image and not say_mod.clean_image(image):
        fail("--image must be an https:// URL", code=2)
    if not template and not (text or "").strip():
        fail('say what? give the line in quotes — navig obs show say "SCHEMA deployed." '
             "— or use --template deploy", code=2)
    s = load_settings()
    # A dry run never touches OBS: it uses --lang or obs.show.say_lang.
    stage = voices.norm_lang(lang) or (None if dry_run else _stage_lang())
    code, _ = say_mod.voice_for(s, stage)
    if template == "deploy":
        text = text or voices.DEPLOY[code]
        tone = "deploy" if tone == "info" else tone
    plan = say_mod.plan_for(
        s,
        (text or "").strip(),
        lang=code,
        provider=provider,
        voice=voice,
        title=title,
        kicker=kicker,
        tone=tone,
        image=image,
        voiced=not no_voice,
        fresh=fresh,
    )
    if not dry_run:
        plan = say_mod.prepare(plan)
        for w in plan.warnings:
            console.print(f"[yellow]○[/yellow] {w}")
        if plan.voiced and plan.clip is None:
            console.print("[yellow]○[/yellow] no voice could speak it — showing the card silently")
        _with_obs(
            lambda c: say_mod.perform(
                c,
                plan,
                toast_event=s.toast_event,
                pigeon_event=s.pigeon_event,
                cue_input=s.cue_input,
            )
        )
    data = {
        **plan.to_json(),
        "dryRun": dry_run,
        "cueInput": s.cue_input,
        "stageLang": stage,
    }

    def _render(d: dict) -> None:
        clip = d["clip"] or "[dim]none[/dim]"
        if d["clip"]:
            clip += (
                " (cached)"
                if d["cached"]
                else (" (new)" if not dry_run else " (to synthesize)")
            )
        console.print(
            kv_table(
                "obs show say" + (" — dry run" if dry_run else ""),
                [
                    ("card", f"{d['toast']['tone']} · {d['toast']['title']}"),
                    ("language", f"{d['lang']}"
                     + ("" if d["stageLang"] else " [dim](no stage language — default)[/dim]")),
                    (
                        "voice",
                        f"{d['provider']}/{d['voice'] or 'default'}"
                        + (f" · fx {d['fx']}" if d["fx"] else "")
                        if d["voiced"]
                        else "[dim]off[/dim]",
                    ),
                    ("image", d["image"] or "[dim]none[/dim]"),
                    ("clip", clip),
                    ("ms", str(d["ms"])),
                ],
            )
        )
        if not dry_run:
            ok(
                f"said → {s.toast_event} + {s.pigeon_event}"
                + (f" + {s.cue_input}" if d["clip"] else "")
            )

    emit(data, _render, as_json=as_json)


# ── voices: hear the candidates, keep one ────────────────────────────────────
ELEVEN_PIGEON = "N2lVS1w4EtoT3dr4eOWO"  # "Callum — Husky Trickster", premade
SAMPLE_LINE = {
    "en": "SCHEMA deployed. The Grid is awake, and so am I.",
    "fr": "SCHEMA est déployé. La Grille est réveillée, et moi aussi.",
    "ru": "SCHEMA обновлена. Сетка проснулась, и я тоже.",
}


def _sample_directions() -> dict[str, dict[str, voices.Voice]]:
    """The four pigeon directions, per language (the owner picks by ear)."""
    warm = {
        "en": voices.Voice("mimo", "Dean", voices.PIGEON_STYLE["en"]),
        "fr": voices.Voice("edge", "fr-FR-HenriNeural"),
        "ru": voices.Voice("edge", "ru-RU-DmitryNeural"),
    }
    out: dict[str, dict[str, voices.Voice]] = {
        "A-warm": warm,
        "B-cartoon": {k: replace(v, fx="pitch=+3,tempo=1.08") for k, v in warm.items()},
        "C-street": {
            k: replace(
                v,
                fx="pitch=-2,tempo=0.96",
                style=(v.style + " Low, raspy, a night-street voice.") if v.style else None,
            )
            for k, v in warm.items()
        },
        # A premade multilingual MALE voice every ElevenLabs account has (the
        # provider's own default is "Janet", a woman's English voice). For a
        # native French or Russian voice from your library, use --try.
        "D-eleven": {k: voices.Voice("elevenlabs", ELEVEN_PIGEON) for k in voices.LANGS},
    }
    return out


@show_app.command("voices")
def show_voices(
    sample: Annotated[
        bool,
        typer.Option("--sample", help="Render one line per direction × language."),
    ] = False,
    play: Annotated[
        bool,
        typer.Option("--play", help="Play the samples on stream, one by one, with a card."),
    ] = False,
    lang: Annotated[
        list[str], typer.Option("--lang", help="Only these languages (repeatable).")
    ] = [],  # noqa: B006
    direction: Annotated[
        list[str], typer.Option("--direction", help="Only these directions (repeatable).")
    ] = [],  # noqa: B006
    use: Annotated[
        str | None,
        typer.Option("--use", help="Keep a direction as the stage voice (writes obs.show.say_voices)."),
    ] = None,
    try_voice: Annotated[
        list[str],
        typer.Option("--try", help="Hear any voice: PROVIDER[:VOICE] (repeatable), e.g. elevenlabs:<id>."),
    ] = [],  # noqa: B006
    set_voice: Annotated[
        list[str],
        typer.Option(
            "--set",
            help="Keep one language's voice: LANG=PROVIDER[:VOICE] (repeatable); "
            "its free Edge voice stays the fallback.",
        ),
    ] = [],  # noqa: B006
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable JSON.")
    ] = False,
) -> None:
    """The pigeon's voice per language: show it, sample candidates, keep one.

    \b
      navig obs show voices                      the voice each language uses now
      navig obs show voices --sample --play      hear every candidate on stream
      navig obs show voices --use B-cartoon      keep a direction for all languages
    """
    import time

    s = load_settings()
    table = voices.table(s.say_voices)
    dirs = _sample_directions()
    langs = [voices.norm_lang(x) for x in lang] or list(voices.LANGS)
    if any(x is None for x in langs):
        fail(f"--lang must be one of: {', '.join(voices.LANGS)}", code=2)
    def _parse_pv(raw: str) -> voices.Voice:
        provider, _, voice = raw.partition(":")
        if not provider.strip():
            fail(f"'{raw}': expected PROVIDER[:VOICE]", code=2)
        return voices.Voice(provider.strip(), voice.strip() or None)

    if set_voice:
        chosen: dict[str, dict[str, Any]] = {}
        for item in set_voice:
            code, _, pv = item.partition("=")
            code = voices.norm_lang(code) or ""
            if not code or not pv:
                fail(f"--set '{item}': expected LANG=PROVIDER[:VOICE] with LANG en|fr|ru", code=2)
            chosen[code] = _parse_pv(pv).to_json()
        use = "--set"
    elif use:
        if use not in dirs:
            fail(f"--use must be one of: {', '.join(dirs)}", code=2)
        chosen = {k: v.to_json() for k, v in dirs[use].items() if k in langs}
    if use:
        merged = {k: v.to_json() for k, v in table.items()}
        merged.update(chosen)
        import json as _json
        import shutil
        import subprocess

        exe = shutil.which("navig")
        if not exe:
            fail("navig is not on PATH — set obs.show.say_voices by hand")
        res = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [exe, "config", "set", "obs.show.say_voices",
             _json.dumps(merged, ensure_ascii=False)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
        if res.returncode != 0:
            fail(f"navig config set failed: {(res.stderr or res.stdout).strip()[-200:]}")
        ok(f"kept {use} for {', '.join(sorted(chosen))} → obs.show.say_voices")
        return
    rows: list[dict[str, Any]] = []
    if not sample:
        for code in langs:
            v = table[code]
            rows.append({"lang": code, **v.to_json()})
    else:
        if try_voice:
            dirs = {raw: {k: _parse_pv(raw) for k in voices.LANGS} for raw in try_voice}
            direction = list(dirs)
        names = direction or list(dirs)
        for name in names:
            if name not in dirs:
                fail(f"--direction must be one of: {', '.join(dirs)}", code=2)
            for code in langs:
                v = dirs[name][code]
                p = say_mod.plan(
                    SAMPLE_LINE[code], title=f"VOICE {name}", kicker=code.upper(),
                    provider=v.provider, voice=v.voice, lang=code, style=v.style, fx=v.fx,
                )
                p = say_mod.prepare(p)
                rows.append({
                    "direction": name, "lang": code, **v.to_json(),
                    "clip": str(p.clip) if p.clip else None, "ms": p.ms,
                    "warnings": p.warnings,
                })
                if play and p.clip is not None:
                    # One refused clip (OBS busy loading) never ends the run.
                    try:
                        _obs_once(
                            lambda c, p=p: say_mod.perform(
                                c, p, toast_event=s.toast_event,
                                pigeon_event=s.pigeon_event, cue_input=s.cue_input,
                            )
                        )
                        time.sleep(p.ms / 1000 + 1.2)
                    except ObsErrorAll as exc:
                        rows[-1]["warnings"] = [*rows[-1]["warnings"], f"not played: {exc}"]

    def _render(d: dict) -> None:
        for r in d["rows"]:
            head = f"{r.get('direction', 'now'):>9} · {r['lang']}"
            v = f"{r['provider']}/{r['voice'] or 'default'}" + (f" · fx {r['fx']}" if r["fx"] else "")
            tail = ""
            if "clip" in r:
                tail = f" → {r['clip'] or '[red]' + '; '.join(r['warnings']) + '[/red]'}"
                if r["clip"] and r["warnings"]:
                    tail += f" [yellow]({'; '.join(r['warnings'])})[/yellow]"
            console.print(f"{head}  {v}{tail}")

    emit({"rows": rows}, _render, as_json=as_json)


# ── flash: Schema TV calls a flash mission on air ────────────────────────────
FLASH_REFUSALS = {
    "module-off": "flash missions are switched off on the switchboard",
    "missions-closed": "this Zone's missions are not released yet",
    "too-quiet": "too few active citizens in this Zone for a flash",
    "already-live": "a flash is already running in this Zone",
    "already-today": "this Zone already had its on-air flash today",
}
SESSION_PROVIDER = "schema_operator_session"


def _operator_session() -> str | None:
    """An Architect's session token for the Schema API (vault, never printed)."""
    import os

    env = os.environ.get("NAVIG_SCHEMA_SESSION")
    if env:
        return env.strip()
    try:
        from navig_vault.core import get_vault  # lazy: standalone has no navig

        key = get_vault().get_api_key(SESSION_PROVIDER)
        return str(key).strip() or None if key else None
    except Exception:  # noqa: BLE001
        return None


@show_app.command("flash")
def show_flash(
    zone: Annotated[str, typer.Argument(help="The Zone's slug (e.g. saint-petersburg).")],
    api: Annotated[
        str,
        typer.Option("--api", help="Schema API base (default: the civic feed's host)."),
    ] = "",
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable JSON.")
    ] = False,
) -> None:
    """Call a flash mission on air for one Zone (the Architects' flash door).

    It deals a REAL mission and tells that Zone's citizens — the operator's
    act, never automatic. Needs an Architect session in the vault
    (provider schema_operator_session). The stage shows it through the civic
    feed; with obs.show.civic_say on, the pigeon announces it.
    """
    import json as _json
    import urllib.error
    import urllib.parse
    import urllib.request

    s = load_settings()
    base = api.strip()
    if not base:
        u = urllib.parse.urlsplit(s.civic_feed or "https://api.schema.cx/api/civic/events")
        base = f"{u.scheme}://{u.netloc}"
    token = _operator_session()
    if not token:
        fail(
            f"no Architect session: store one with `navig vault set {SESSION_PROVIDER} "
            "--key api_key` (an Outpost sign-in's session token)",
            code=2,
        )
    req = urllib.request.Request(  # noqa: S310 — https/http base chosen by the operator
        base.rstrip("/") + "/api/admin/flash/deal",
        data=_json.dumps({"slug": zone}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as res:  # noqa: S310
            body = _json.loads(res.read().decode("utf-8") or "{}")
            status = res.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            body = _json.loads(exc.read().decode("utf-8") or "{}")
        except ValueError:
            body = {}
    except (urllib.error.URLError, OSError) as exc:
        fail(f"Schema API unreachable ({exc})")
        return
    data = {"status": status, "zone": zone, **(body if isinstance(body, dict) else {})}

    def _render(d: dict) -> None:
        if d["status"] == 200:
            ok(f"flash on air in {zone}")
        elif d["status"] == 409:
            reason = str(d.get("refused") or "")
            console.print(f"[yellow]○[/yellow] refused: {FLASH_REFUSALS.get(reason, reason)}")
        elif d["status"] == 404:
            console.print("[red]✗[/red] no such Zone — or this session is not an Architect's")
        else:
            console.print(f"[red]✗[/red] the flash door answered {d['status']}")

    emit(data, _render, as_json=as_json)
    if status != 200:
        raise typer.Exit(1)
