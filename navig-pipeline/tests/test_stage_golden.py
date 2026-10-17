"""Golden scenes — real shipped episodes checked whenever the stage changes.

`fixtures/stage/` holds three scenes exactly as they shipped (with their real, redacted
casts): E05 (a 3-line caption over a tall panel — the collision case), E07 (the
stranded-"files." cue case) and E02 (a long command that must wrap at a space). Before
these, no stage scene was tracked in the repo, so a stage change that broke a layout was
found only after a 20-minute render, by eye. Now the plugin suite — which the land gate
runs whenever `navig_pipeline` changes — lints them always and measures their layout in a
headless browser when one is available.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from navig_pipeline import cast as C
from navig_pipeline import stage as S
from navig_pipeline.qa import check_layout, lint_scene

GOLDEN = Path(__file__).parent / "fixtures" / "stage"
SCENES = sorted(GOLDEN.glob("*/scene.json"))


def test_the_golden_set_is_not_empty() -> None:
    # a check over zero scenes passes vacuously — the floor is the three we ship
    assert {p.parent.name for p in SCENES} >= {"e02", "e05", "e07"}


@pytest.mark.parametrize("scene_path", SCENES, ids=lambda p: p.parent.name)
def test_golden_scenes_lint_clean(scene_path: Path) -> None:
    scene = S.load_scene(scene_path)
    headers = {i: C.load(scene_path.parent / seg["cast"]).header
               for i, seg in enumerate(scene["segments"]) if seg.get("cast")}
    assert lint_scene(scene, headers) == []


def _browser():
    try:
        from navig_browser import cdp_actions
    except ImportError:
        return None
    return cdp_actions if shutil.which("chrome") or Path(
        r"C:\Program Files\Google\Chrome\Application\chrome.exe").exists() else None


@pytest.mark.skipif(_browser() is None, reason="no Chrome / navig-browser to measure the stage with")
def test_golden_scenes_have_no_caption_collision_or_unsafe_box(tmp_path: Path) -> None:
    from navig_browser import cdp_actions
    from navig_browser.session_manager import get_session_manager

    from navig_pipeline.qa import measure_layout

    port = cdp_actions.new(app="chrome", headless=True, window_size="1080x1920").get("port")
    assert port, "could not launch a headless browser"
    findings = {}
    try:
        async def measure_all():
            try:
                for scene_path in SCENES:
                    page = S.build(scene_path, tmp_path / f"{scene_path.parent.name}.stage.html")
                    boxes = await measure_layout(page, len(S.load_scene(scene_path)["segments"]), port=port)
                    findings[scene_path.parent.name] = check_layout(boxes)
            finally:
                await get_session_manager().release(port)

        asyncio.run(measure_all())
    finally:
        cdp_actions.stop(port=port)
    assert {k: [f"{f.where} {f.rule}: {f.message}" for f in v] for k, v in findings.items() if v} == {}
