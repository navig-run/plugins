"""The Reel Stage — scenes compile into one self-contained page a reel can film.

What is pinned: a scene the player would render black is refused up front; a terminal
segment cannot exist without a real capture; the compiled page needs nothing beside itself
(Chrome blocks fetch() from file://); a `stage:` shot resolves to a fresh page with its slot
length; and the stage's colours stay those of @navig/tokens.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from navig_pipeline import cast as C
from navig_pipeline import stage as S
from navig_pipeline.reel import ReelError, capture_url


@pytest.fixture()
def studio(tmp_path: Path) -> Path:
    C.save(C.Cast(header={"width": 44, "command": "navig devnet ssl navig.run"},
                  events=[(0.4, "o", "\x1b[32m✓\x1b[0m ok\r\n")]), tmp_path / "ssl.cast")
    (tmp_path / "pic.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    scene = {
        "title": "t", "episode": "S01·E01",
        "segments": [
            {"type": "hook", "title": "Hook *line*"},
            {"type": "terminal", "cast": "ssl.cast", "caption": "</script><b>x</b>"},
            {"type": "telegram", "script": [{"me": "/ssl"}, {"bot": "ok", "image": "pic.svg"}]},
            {"type": "outro"},
        ],
    }
    (tmp_path / "ep.scene.json").write_text(json.dumps(scene), encoding="utf-8")
    return tmp_path


# ── validation ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("scene, needle", [
    ({}, "segments"),
    ({"segments": []}, "segments"),
    ({"segments": [{"type": "slideshow"}]}, "slideshow"),
    ({"segments": [{"type": "hook"}]}, "title"),
    ({"segments": [{"type": "image"}]}, "image"),
    ({"segments": [{"type": "terminal"}]}, "navig pipeline cast"),
    ({"segments": [{"type": "telegram", "script": []}]}, "script"),
    ({"segments": [{"type": "telegram", "script": [{"say": "hi"}]}]}, "me/bot/tap/wait"),
])
def test_validate_refuses_what_would_render_broken(scene: dict, needle: str) -> None:
    with pytest.raises(S.StageError, match=needle):
        S.validate(scene)


# ── compile + page ──────────────────────────────────────────────────────────


def test_compile_inlines_casts_and_images(studio: Path) -> None:
    compiled = S.compile_scene(S.load_scene(studio / "ep.scene.json"), studio)
    term = compiled["segments"][1]
    assert term["cast"]["header"]["width"] == 44
    assert term["cast"]["events"] == [[0.4, "\x1b[32m✓\x1b[0m ok\r\n"]]
    assert compiled["assets"]["pic.svg"].startswith("data:image/svg+xml;base64,")
    for name in S.BUILTIN_ART:
        assert compiled["assets"][name].startswith("data:")


def test_compile_does_not_mutate_the_scene(studio: Path) -> None:
    scene = S.load_scene(studio / "ep.scene.json")
    S.compile_scene(scene, studio)
    assert scene["segments"][1]["cast"] == "ssl.cast"


def test_missing_cast_and_image_are_named(studio: Path) -> None:
    scene = S.load_scene(studio / "ep.scene.json")
    scene["segments"][1]["cast"] = "nope.cast"
    with pytest.raises(S.StageError, match="nope.cast"):
        S.compile_scene(scene, studio)
    scene = S.load_scene(studio / "ep.scene.json")
    scene["segments"][2]["script"][1]["image"] = "gone.png"
    with pytest.raises(S.StageError, match="gone.png"):
        S.compile_scene(scene, studio)


def test_page_is_self_contained(studio: Path) -> None:
    page = S.build(studio / "ep.scene.json")
    html = page.read_text(encoding="utf-8")
    assert page.name == "ep.stage.html"
    assert "{{" not in html
    assert "window.__SCENE" in html and "--primary" in html
    # nothing fetched from disk at runtime: no relative src/href survives
    assert 'src="ssl.cast"' not in html and "pic.svg\"" not in html.split("window.__SCENE")[0]
    # scene text cannot close the script tag it lives in
    assert "</script><b>" not in html


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_player_script_parses() -> None:
    src = (S.ASSETS / "stage.js").read_text(encoding="utf-8")
    proc = subprocess.run(["node", "-e", "new Function(require('fs').readFileSync(0,'utf8'))"],
                          input=src, capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr


# ── stage: shots ────────────────────────────────────────────────────────────


def test_stage_url_builds_the_page_with_its_slot(studio: Path) -> None:
    url = S.resolve_stage_url("stage:ep.scene.json#1", base_dir=studio, secs=3.5)
    assert url.startswith("file:") and url.endswith("ep.stage.html?seg=1&dur=3.500")
    assert (studio / "ep.stage.html").exists()


def test_stage_url_rebuilds_only_when_a_source_is_newer(studio: Path) -> None:
    S.resolve_stage_url("stage:ep.scene.json", base_dir=studio)
    page = studio / "ep.stage.html"
    # Anchored in the FUTURE: the player's own assets are sources too, and their mtime is
    # whenever the package was last edited — a timestamp in the past races that.
    future = time.time() + 3600
    os.utime(page, (future, future))
    stamp = page.stat().st_mtime
    S.resolve_stage_url("stage:ep.scene.json", base_dir=studio)
    assert page.stat().st_mtime == stamp  # fresh: untouched
    os.utime(studio / "ssl.cast", (future + 60, future + 60))  # a recapture lands
    S.resolve_stage_url("stage:ep.scene.json", base_dir=studio)
    assert page.stat().st_mtime != stamp  # rebuilt


def test_reel_capture_url_templates_and_delegates(studio: Path) -> None:
    assert capture_url("http://h/?d={secs}&s={w}x{h}", base_dir=studio, secs=2.0,
                       width=1080, height=1920) == "http://h/?d=2.000&s=1080x1920"
    assert "seg=0" in capture_url("stage:ep.scene.json#0", base_dir=studio, secs=2.0,
                                  width=1080, height=1920)
    with pytest.raises(ReelError, match="missing.json"):
        capture_url("stage:missing.json#0", base_dir=studio, secs=2.0, width=1080, height=1920)


# ── tokens ──────────────────────────────────────────────────────────────────


@pytest.mark.skipif(not S.TOKENS_SOURCE.exists(), reason="not a monorepo checkout")
def test_stage_tokens_match_navig_tokens() -> None:
    assert S.tokens_css() == S.sync_tokens(), (
        "stage_assets/tokens.css drifted from packages/shared/navig-tokens — "
        "regenerate with: py -3.13 -m navig_pipeline.stage --sync-tokens"
    )


def test_embedded_fonts_produce_data_uri_faces() -> None:
    """Fonts found on this machine are inlined; the CSS still names a fallback stack.

    A video must not depend on the network or on a Google Fonts fetch — if a font is
    missing the page still builds (no block, fallback stack), it just renders in the
    fallback face. This asserts the inlining path itself: at least the slab face on a
    machine that has Bebas, and always-valid CSS.
    """
    css = S.fonts_css()
    assert "@font-face" in css and "{{" not in css
    for block in css.splitlines():
        assert "base64," in block and "font-display:block" in block
    faces = css.count("@font-face")
    assert faces >= (1 if S._find_font([n for _, _, _, n in S.FONT_FACES][0]) else 0)
    assert faces <= len(S.FONT_FACES) + len(S.GEIST_GLOBS)


def test_stage_imports_from_a_shallow_path() -> None:
    """site-packages is ~4 ancestors deep; `parents[8]` is not.

    The font-root walk once indexed fixed parents, so importing this module under a
    SHALLOW path raised IndexError — on main's checkout depth that broke every
    `stage`/`clip` render while the deep worktree's own tests stayed green. The copy is
    imported in a subprocess so the module cache of this session is untouched.
    """
    import shutil
    import subprocess
    import sys
    import tempfile

    # %TEMP%\sp<n>\navig_pipeline → 8 ancestors (0..7): parents[8] would raise there.
    root = Path(tempfile.mkdtemp(prefix="stage-shallow-", dir=os.environ.get("TEMP")))
    try:
        shutil.copytree(Path(S.__file__).parent, root / "navig_pipeline")
        code = (
            f"import sys; sys.path.insert(0, {str(root)!r});"
            "import navig_pipeline.stage as s;"
            "assert (s.ASSETS / 'stage.html').exists();"
            "print('ok', len(s._FONT_ROOTS))"
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, encoding="utf-8", timeout=120)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip().endswith("ok 0")  # installed: no repo roots, and that is fine
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── typography guards ───────────────────────────────────────────────────────
# Frame QA on 2026-10-15 found three design bugs the schema pass introduced: a command
# typed over a long package name split mid-token (`com.samsung.androi` / `d.app.notes`),
# a 3-line caption at 66px collided with the terminal panel's top edge (E05), and the
# karaoke line measured empty at fit() time so the panel centred into where the cue was
# about to land. These pin the rules, not the pixels.


def test_command_row_wraps_at_spaces_not_mid_token() -> None:
    css = (S.ASSETS / "stage.css").read_text(encoding="utf-8")
    rule = next(line for line in css.splitlines() if ".row.cmdrow" in line)
    assert "overflow-wrap: break-word" in rule
    assert "break-all" not in rule


def test_caption_band_is_measured_at_its_worst_case() -> None:
    js = (S.ASSETS / "stage.js").read_text(encoding="utf-8")
    # the longest cue is pre-rendered before fit() measures the band
    assert "LONGEST cue" in js
    # and the panel never centres up under the caption it measured
    assert "capBottom" in js and "capBottom + 24" in js


def test_stage_exposes_its_settled_layout_for_preflight_qa() -> None:
    js = (S.ASSETS / "stage.js").read_text(encoding="utf-8")
    # `navig pipeline qa --layout` reads this; renaming it silently disables the probe
    assert "layout: () => ready.then(layout)" in js
    assert 'querySelector(".cap")' in js and '".term, .tg, .img, .frame"' in js


def test_a_pushed_down_panel_is_shrunk_above_the_caption_bar() -> None:
    js = (S.ASSETS / "stage.js").read_text(encoding="utf-8")
    assert "if (top + h > 1500) term.style.height" in js

