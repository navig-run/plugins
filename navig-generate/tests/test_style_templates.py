"""Style-template catalogue: bundled wave1/styles.json stays loadable and sane."""
from __future__ import annotations

import json
from pathlib import Path

CAT = Path(__file__).resolve().parents[1] / "navig_generate" / "styles.json"


def _load() -> dict:
    return json.loads(CAT.read_text(encoding="utf-8"))


def test_catalog_loads_ids_unique_and_key_ids_present() -> None:
    data = _load()
    assert data["schema"] == "navig-style-templates/v1"
    ids = [s["id"] for s in data["styles"]]
    assert len(ids) == len(set(ids)), "duplicate style ids"
    assert {"INS-01", "CYB-03", "LAY-02", "NMD-10", "MKT-06"} <= set(ids)
    assert len(ids) >= 30


def test_image_styles_carry_prompt_motion_and_no_text_guard() -> None:
    for s in _load()["styles"]:
        if s.get("kind") != "image":
            continue
        assert s.get("prompt"), f"{s['id']} missing prompt"
        assert s.get("motion"), f"{s['id']} missing motion (side effects)"
        p = s["prompt"].lower()
        assert (
            "no text" in p or "no readable text" in p or "no letters" in p
        ), f"{s['id']} missing no-text guard"


def test_layout_styles_carry_layout_block() -> None:
    for s in _load()["styles"]:
        if s.get("kind") == "layout":
            assert s.get("layout"), f"{s['id']} missing layout block"


def test_loader_from_cli_module() -> None:
    from navig_generate.commands.generate import _load_styles  # noqa: PLC0415

    assert len(_load_styles()["styles"]) >= 30
