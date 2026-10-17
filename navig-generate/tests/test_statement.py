"""``navig generate statement`` — the overlay contract, and the CLI's edges.

The transform (``*red word*`` → colour-split markers) is unit-tested directly;
the render is exercised end-to-end on a real spec (Druk/Fira fall back rather
than fail, so a fontless box still produces a PNG — the byte-parity run against
the channel's original renderer is the strong evidence and lives in the PR
notes, run on a machine with the channel's fonts).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from navig_generate.commands.generate import generate_app as standalone_app
from navig_generate.statement import H, SPLIT, W, prepare_line, scanlines

runner = CliRunner()
PIL = pytest.importorskip("PIL", reason="Pillow not installed")

SPEC = {
    "line": "You are not supposed to *see this*",
    "sub": "266 of 268 cities have never been claimed.",
    "tag": "Outpost",
    "size": 86,
    "bip": True,
}


# ── the transform ────────────────────────────────────────────────────────────
def test_prepare_line_turns_red_markers_into_split_parts():
    out = prepare_line(SPEC)
    assert out["line"].count(SPLIT) == 2, out["line"]
    assert "*" not in out["line"]
    # alternating parts: prefix / red segment / (trailing marker becomes empty)
    parts = out["line"].split(SPLIT)
    assert parts[0] == "You are not supposed to "
    assert parts[1] == "see this"
    assert parts[2] == ""


def test_prepare_line_handles_plain_and_empty_lines():
    assert prepare_line({"line": "Plain"})["line"] == "Plain"
    assert prepare_line({}).get("line") == ""


# ── render + CLI ─────────────────────────────────────────────────────────────
def _write_spec(tmp_path: Path, spec: dict) -> Path:
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(spec), encoding="utf-8")
    return p


def test_render_produces_a_full_frame(tmp_path: Path):
    from navig_generate.statement import render

    out = tmp_path / "out.png"
    path = render(prepare_line(SPEC), out, mark=None)
    assert path == out and out.is_file()
    from PIL import Image

    with Image.open(out) as im:
        assert im.size == (W, H)


def test_red_segments_change_the_output(tmp_path: Path):
    from navig_generate.statement import render

    a, b = tmp_path / "a.png", tmp_path / "b.png"
    render(prepare_line({"line": "A *RED* B", "hud": False, "grid": False}), a, None)
    render(prepare_line({"line": "A RED B", "hud": False, "grid": False}), b, None)
    assert a.read_bytes() != b.read_bytes(), "the colour split must change pixels"


def test_cli_writes_the_png(tmp_path: Path):
    spec = _write_spec(tmp_path, SPEC)
    out = tmp_path / "sub" / "out.png"
    res = runner.invoke(standalone_app,
                        ["statement", str(spec), str(out), "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["size"] == [1080, 1920]
    assert Path(payload["out"]).is_file()


def test_cli_missing_spec_exits_2(tmp_path: Path):
    res = runner.invoke(standalone_app,
                        ["statement", str(tmp_path / "nope.json"), str(tmp_path / "o.png")])
    assert res.exit_code == 2


def test_cli_bad_json_exits_2(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    res = runner.invoke(standalone_app, ["statement", str(bad), str(tmp_path / "o.png")])
    assert res.exit_code == 2
    assert "JSON" in res.output


def test_cli_non_object_spec_exits_2(tmp_path: Path):
    lst = tmp_path / "list.json"
    lst.write_text("[1, 2]", encoding="utf-8")
    res = runner.invoke(standalone_app, ["statement", str(lst), str(tmp_path / "o.png")])
    assert res.exit_code == 2


def test_scanlines_layer_is_the_right_size(tmp_path: Path):
    p = tmp_path / "scan.png"
    scanlines(p)
    from PIL import Image

    with Image.open(p) as im:
        assert im.size == (W, H)
        assert im.getpixel((0, 0))[3] > 0  # period=4 → a line at y=0


# ── fonts ────────────────────────────────────────────────────────────────────
def test_missing_fonts_names_an_absent_face(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from navig_generate import statement

    monkeypatch.setattr(statement, "DRUK", str(tmp_path / "nope.otf"))
    assert "Druk-Medium.otf" in statement.missing_fonts()


def test_cli_warns_and_still_renders_when_a_face_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A non-fallback face missing: warn (parity lost) but exit 0 with a PNG."""
    from navig_generate import statement

    monkeypatch.setattr(statement, "DRUK", str(tmp_path / "nope.otf"))
    spec = _write_spec(tmp_path, SPEC)
    out = tmp_path / "fallback.png"
    res = runner.invoke(standalone_app, ["statement", str(spec), str(out), "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)  # the warning is suppressed in --json mode
    assert "Druk-Medium.otf" in payload["font_warnings"]
    assert Path(payload["out"]).is_file()


def test_cli_refuses_when_the_fallback_face_itself_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Inter-SemiBold IS the fallback — without it nothing can render at all."""
    from navig_generate import statement

    monkeypatch.setattr(statement, "INTER_SB", str(tmp_path / "nope.ttf"))
    spec = _write_spec(tmp_path, SPEC)
    res = runner.invoke(standalone_app, ["statement", str(spec), str(tmp_path / "o.png"), "--json"])
    assert res.exit_code == 1, res.output
    assert "Inter-SemiBold" in res.output
