"""The Reel Stage — a vertical, phone-native canvas for reel and clip shots.

A scene (JSON) is a list of segments — ``hook``, ``terminal``, ``telegram``, ``image``,
``outro`` —
that :func:`build` compiles into ONE self-contained HTML page: the player, the stylesheet,
NAVIG's design tokens, every cast and every image inlined. Self-contained because Chrome
refuses ``fetch()`` from a ``file://`` page, and because a single file previews by
double-click, renders offline, and can be handed to someone else.

A reel shot films it through the ordinary ``url=`` path, with a ``stage:`` shorthand::

    [shot: url="stage:ep04.scene.json#1" secs=4]

:func:`resolve_stage_url` rebuilds the page when the scene or a cast is newer, and points
the capture at segment 1 with the slot length (``dur=``) the reel planned, so the player can
fit itself to it. See ``stage_assets/stage.js`` for the URL contract.

Terminal segments replay a real capture (:mod:`navig_pipeline.cast`); nothing here writes
terminal output.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from navig_pipeline import cast as castmod

ASSETS = Path(__file__).with_name("stage_assets")
SEGMENT_TYPES = {"hook", "terminal", "telegram", "image", "outro"}
# Built-in art a scene may name instead of a path.
BUILTIN_ART = {"kraken": "kraken.png", "avatar": "avatar.png", "mark": "mark.svg"}
# Fields that name an image: a built-in name, an `assets:` key, or a path next to the scene.
_IMAGE_FIELDS = ("art", "image")
# Ancestors are WALKED, never indexed: a fixed `parents[N]` exists in this repo's deep
# path but raises IndexError under site-packages — which is exactly how this module once
# failed to import on main, taking every stage/clip render with it.
_HERE = Path(__file__).resolve()
_TOKENS_CANDIDATE = Path("packages") / "shared" / "navig-tokens" / "index.css"
TOKENS_SOURCE = next(
    (p / _TOKENS_CANDIDATE for p in _HERE.parents if (p / _TOKENS_CANDIDATE).is_file()),
    _HERE.parents[min(3, len(_HERE.parents) - 1)] / _TOKENS_CANDIDATE,  # absent → tests skip
)

# ── fonts ───────────────────────────────────────────────────────────────────
#
# A rendered video must not depend on the network or on what the machine happens to have
# installed, so the fonts are EMBEDDED into the compiled page as base64 at build time.
# They are read from this machine, never shipped in the package (the published wheel would
# otherwise carry font files with their own licence) — a machine without them gets the CSS
# fallback stack and the render still works, just in the fallback type.
FONT_DIRS = [
    Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "Windows" / "Fonts"
    if os.environ.get("LOCALAPPDATA") else Path.home() / "AppData/Local/Microsoft/Windows/Fonts",
    Path.home() / "AppData" / "Local" / "Microsoft" / "Windows" / "Fonts",
    Path("C:/Windows/Fonts"),
    Path.home() / ".fonts",
    Path("/usr/share/fonts/truetype"),
]
# (css family, weight, format, [filenames to look for]) — searched in FONT_DIRS, first hit wins.
FONT_FACES = [
    ("Slam", 700, "truetype", ["BebasNeue Bold.ttf"]),           # kinetic headlines (schema-style)
    ("JetBrains Mono", 500, "truetype", ["JetBrainsMono-Medium.ttf"]),
    ("JetBrains Mono", 700, "truetype", ["JetBrainsMono-Bold.ttf"]),
]
# Geist is the product face (web/www); a repo checkout supplies it, an install may not.
GEIST_GLOBS = [
    ("Geist", 100, 900, "woff2", "geist-latin-wght-normal-*.woff2"),
    ("Geist Mono", 100, 900, "woff2", "geist-mono-latin-wght-normal-*.woff2"),
]
# Every checkout above this file that has apps/os — the worktree AND the main checkout
# (whose built webui carries Geist as a build artifact a fresh worktree does not).
# Walked to exhaustion: an empty list (installed from site-packages) is a valid answer,
# an IndexError is not.
_FONT_ROOTS = [p for p in _HERE.parents if (p / "apps" / "os").is_dir()]


class StageError(ValueError):
    """The scene cannot be staged — unknown segment, missing cast, missing image."""


# ── scene ───────────────────────────────────────────────────────────────────


def load_scene(path: Path) -> dict[str, Any]:
    path = Path(path)
    try:
        scene = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StageError(f"scene not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise StageError(f"{path.name} is not valid JSON: {exc}") from exc
    validate(scene, where=path.name)
    return scene


def validate(scene: Any, *, where: str = "scene") -> None:
    """Reject a scene the player would render as a black or broken frame."""
    if not isinstance(scene, dict) or not isinstance(scene.get("segments"), list) or not scene["segments"]:
        raise StageError(f"{where}: needs a non-empty \"segments\" list")
    for i, seg in enumerate(scene["segments"]):
        kind = seg.get("type") if isinstance(seg, dict) else None
        if kind not in SEGMENT_TYPES:
            raise StageError(f"{where}: segment {i} has type {kind!r} — use one of {sorted(SEGMENT_TYPES)}")
        if kind == "image" and not seg.get("image"):
            raise StageError(f"{where}: image segment {i} needs an \"image\"")
        if kind == "hook" and not seg.get("title"):
            raise StageError(f"{where}: hook segment {i} needs a \"title\"")
        if kind == "terminal" and not seg.get("cast"):
            raise StageError(
                f"{where}: terminal segment {i} needs a \"cast\" — capture the real output with "
                "`navig pipeline cast -o x.cast -- <command>`; terminal text is never typed by hand"
            )
        sp = seg.get("speech")
        if sp is not None and (not isinstance(sp, dict)
                               or not isinstance(sp.get("cues"), list)
                               or not isinstance(sp.get("words"), list)):
            raise StageError(
                f"{where}: speech on segment {i} must be {{\"cues\": [[text, t0, t1]…], "
                "\"words\": [[word, s, e]…]}} in section-local seconds"
            )
        if kind == "telegram":
            script = seg.get("script")
            if not isinstance(script, list) or not script:
                raise StageError(f"{where}: telegram segment {i} needs a non-empty \"script\"")
            for j, step in enumerate(script):
                if not isinstance(step, dict) or not ({"me", "bot", "tap", "wait"} & step.keys()):
                    raise StageError(f"{where}: telegram segment {i} step {j} needs me/bot/tap/wait")


def _data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if path.suffix.lower() == ".svg":
        mime = "image/svg+xml"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def _inputs(scene: dict[str, Any], base: Path) -> list[Path]:
    """Every file the compiled page depends on (for staleness checks)."""
    paths = []
    for seg in scene["segments"]:
        if seg.get("type") == "terminal" and isinstance(seg.get("cast"), str):
            paths.append(base / seg["cast"])
    for value in (scene.get("assets") or {}).values():
        paths.append(base / value)
    return paths


def compile_scene(scene: dict[str, Any], base: Path) -> dict[str, Any]:
    """Inline casts and images so the page needs nothing beside itself."""
    out = json.loads(json.dumps(scene))  # deep copy; the source scene is never mutated
    assets: dict[str, str] = {name: _data_uri(ASSETS / file) for name, file in BUILTIN_ART.items()}
    for name, rel in (scene.get("assets") or {}).items():
        p = base / rel
        if not p.exists():
            raise StageError(f"asset {name!r} not found: {p}")
        assets[name] = _data_uri(p)

    def image(ref: str) -> None:
        if ref in assets or ref.startswith("data:"):
            return
        p = base / ref
        if not p.exists():
            raise StageError(f"image not found: {p} (or name a built-in: {', '.join(BUILTIN_ART)})")
        assets[ref] = _data_uri(p)

    for i, seg in enumerate(out["segments"]):
        for key in _IMAGE_FIELDS:
            if isinstance(seg.get(key), str):
                image(seg[key])
        bot = seg.get("bot")
        if isinstance(bot, dict) and isinstance(bot.get("avatar"), str):
            image(bot["avatar"])
        for step in seg.get("script") or []:
            if isinstance(step.get("image"), str):
                image(step["image"])
        if seg.get("type") == "terminal" and isinstance(seg.get("cast"), str):
            p = base / seg["cast"]
            try:
                cast = castmod.load(p)
            except FileNotFoundError as exc:
                raise StageError(f"segment {i}: cast not found: {p}") from exc
            except castmod.CastError as exc:
                raise StageError(f"segment {i}: {p.name}: {exc}") from exc
            seg["cast"] = {
                "header": {"width": cast.width, "command": cast.command},
                "events": [[t, data] for t, kind, data in cast.events if kind == "o"],
            }
    out["assets"] = assets
    return out


# ── page ────────────────────────────────────────────────────────────────────


def tokens_css() -> str:
    return (ASSETS / "tokens.css").read_text(encoding="utf-8")


def _find_font(names: list[str]) -> Path | None:
    for d in FONT_DIRS:
        for name in names:
            try:
                if (d / name).is_file():
                    return d / name
            except OSError:  # pragma: no cover - unreadable font dir
                continue
    return None


def fonts_css() -> str:
    """``@font-face`` blocks with the font bytes embedded as data URIs.

    Files found on THIS machine are inlined; a missing font contributes no block, so the
    CSS stack falls back (see stage.css) rather than the page failing to build. Fixed:
    `family;weight;size` → the base64 of that file, so a rebuild after an edit does not
    re-encode every font.
    """
    out: list[str] = []
    for family, weight, fmt, names in FONT_FACES:
        path = _find_font(names)
        if path is None:
            continue
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        out.append(
            f"@font-face{{font-family:'{family}';font-weight:{weight};font-display:block;"
            f"src:url(data:font/{fmt};base64,{b64}) format('{fmt}');}}"
        )
    for family, w_lo, w_hi, fmt, pattern in GEIST_GLOBS:
        hits: list[Path] = []
        for root in _FONT_ROOTS:
            hits.extend(root.glob(f"apps/os/**/assets/{pattern}"))
            if hits:
                break
        if not hits:
            continue
        b64 = base64.b64encode(hits[0].read_bytes()).decode("ascii")
        out.append(
            f"@font-face{{font-family:'{family}';font-weight:{w_lo} {w_hi};font-display:block;"
            f"src:url(data:font/{fmt};base64,{b64}) format('{fmt}');}}"
        )
    return "\n".join(out)


def sync_tokens(source: Path = TOKENS_SOURCE) -> str:
    """The ``:root`` block of @navig/tokens, as the stage's committed snapshot holds it.

    The tokens file also carries Tailwind directives (``@theme``, ``@custom-variant``) a
    plain page cannot load, so only the custom properties are lifted.
    """
    src = source.read_text(encoding="utf-8")
    m = re.search(r":root\s*\{.*?\n\}", src, re.S)
    if not m:
        raise StageError(f"no :root block in {source}")
    return (
        "/* GENERATED from packages/shared/navig-tokens/index.css (:root block) — do not edit.\n"
        "   Regenerate: py -3.13 -m navig_pipeline.stage --sync-tokens ; drift is a test failure. */\n"
        + m.group(0) + "\n"
    )


def render_html(compiled: dict[str, Any]) -> str:
    scene_json = json.dumps(compiled, ensure_ascii=False).replace("</", "<\\/")
    page = (ASSETS / "stage.html").read_text(encoding="utf-8")
    title = str(compiled.get("title") or "NAVIG reel")
    # str.replace, not format(): CSS and JS are full of braces.
    for key, value in (
        ("{{FONTS}}", fonts_css()),
        ("{{TOKENS}}", tokens_css()),
        ("{{CSS}}", (ASSETS / "stage.css").read_text(encoding="utf-8")),
        ("{{JS}}", (ASSETS / "stage.js").read_text(encoding="utf-8")),
        ("{{TITLE}}", title.replace("<", "&lt;")),
        ("{{SCENE}}", scene_json),
    ):
        page = page.replace(key, value)
    return page


def default_output(scene_path: Path) -> Path:
    scene_path = Path(scene_path)
    stem = scene_path.name.removesuffix(".json").removesuffix(".scene")
    return scene_path.with_name(f"{stem}.stage.html")


def build(scene_path: Path, out: Path | None = None) -> Path:
    """Compile ``scene_path`` into a self-contained page; returns its path."""
    scene_path = Path(scene_path).resolve()
    scene = load_scene(scene_path)
    target = Path(out) if out else default_output(scene_path)
    html = render_html(compile_scene(scene, scene_path.parent))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(html.encode("utf-8"))
    return target


def is_stale(scene_path: Path, page: Path) -> bool:
    if not page.exists():
        return True
    built = page.stat().st_mtime
    sources = [scene_path, ASSETS / "stage.js", ASSETS / "stage.css", ASSETS / "stage.html"]
    try:
        sources += _inputs(load_scene(scene_path), scene_path.parent)
    except StageError:
        return True
    return any(p.exists() and p.stat().st_mtime > built for p in sources)


def page_url(page: Path, **query: Any) -> str:
    params = {k: v for k, v in query.items() if v is not None}
    q = urlencode({k: (f"{v:.3f}" if isinstance(v, float) else v) for k, v in params.items()})
    return Path(page).resolve().as_uri() + (f"?{q}" if q else "")


def resolve_stage_url(url: str, *, base_dir: Path, secs: float | None = None) -> str:
    """``stage:<scene.json>[#seg]`` → a ``file://`` URL of the (re)built page.

    The scene path is relative to the scenario/shotlist, like every other shot path.
    """
    ref = url[len("stage:"):]
    scene_ref, _, seg = ref.partition("#")
    scene_path = Path(scene_ref)
    if not scene_path.is_absolute():
        scene_path = (Path(base_dir) / scene_path).resolve()
    page = default_output(scene_path)
    if is_stale(scene_path, page):
        build(scene_path, page)
    return page_url(page, seg=int(seg) if seg.strip() else None, dur=secs)


def segment_count(scene_path: Path) -> int:
    return len(load_scene(scene_path)["segments"])


if __name__ == "__main__":  # pragma: no cover - maintenance entry point
    import sys

    if "--sync-tokens" in sys.argv:
        (ASSETS / "tokens.css").write_bytes(sync_tokens().encode("utf-8"))
        print(f"wrote {ASSETS / 'tokens.css'}")
