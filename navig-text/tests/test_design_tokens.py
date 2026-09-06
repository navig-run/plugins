"""Design-token persistence — the primitive + the ``navig design tokens`` CLI.

Storage is redirected to a temp dir via ``NAVIG_DATA_DIR`` so nothing touches the
real ~/.navig. No LLM, no network.
"""

from __future__ import annotations

import base64
import json

import pytest


@pytest.fixture(autouse=True)
def _isolate_data(monkeypatch, tmp_path):
    monkeypatch.setenv("NAVIG_DATA_DIR", str(tmp_path))


SAMPLE = {
    "colors": ["#ffffff", "#111111", "#12c8ff"],
    "fonts": ["Inter", "monospace"],
    "fontSizes": [12, 14, 16, 24],
    "fontWeights": [500, 700],
    "spacing": [4, 8, 16],
    "radii": [4, 8],
    "shadows": ["0 1px 2px rgba(0,0,0,.2)"],
    "url": "https://example.com",
}


def test_save_writes_json_and_css(tmp_path):
    from navig_text.design_tokens import save_tokens

    res = save_tokens(SAMPLE)
    assert res["count"] == 3 + 4 + 2 + 3 + 2  # colors+sizes+weights+spacing+radii
    js = json.loads((tmp_path / "design" / "tokens.json").read_text(encoding="utf-8"))
    assert js["colors"] == SAMPLE["colors"]
    css = (tmp_path / "design" / "tokens.css").read_text(encoding="utf-8")
    assert "--color-1: #ffffff;" in css
    assert "--font-size-4: 24px;" in css
    assert "--radius-2: 8px;" in css
    assert "--font-family: Inter;" in css


def test_css_sanitizes_injection():
    from navig_text.design_tokens import tokens_to_css

    css = tokens_to_css({"colors": ["#000; } body{display:none} .x{"]})
    # The braces/semicolons that would break out of the declaration are stripped.
    assert "body{display:none}" not in css
    assert "}" == css.strip()[-1]  # only the closing :root brace remains


def test_empty_set_refused():
    from navig_text.design_tokens import save_tokens

    with pytest.raises(ValueError):
        save_tokens({"url": "https://x", "colors": []})


def test_load_roundtrip():
    from navig_text.design_tokens import load_tokens, save_tokens

    assert load_tokens() is None  # nothing saved yet
    save_tokens(SAMPLE)
    assert load_tokens()["fontSizes"] == SAMPLE["fontSizes"]


def test_cli_tokens_save_and_show():
    from typer.testing import CliRunner

    from navig_text.commands.design import design_app

    payload = base64.b64encode(json.dumps(SAMPLE).encode()).decode()
    r = CliRunner().invoke(design_app, ["tokens", "save", "--b64", payload])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output.strip().splitlines()[-1])
    assert data["ok"] is True and data["count"] == 14

    r2 = CliRunner().invoke(design_app, ["tokens", "show", "--json"])
    assert r2.exit_code == 0, r2.output
    shown = json.loads(r2.output.strip().splitlines()[-1])
    assert shown["ok"] is True
    assert shown["tokens"]["colors"] == SAMPLE["colors"]


def test_cli_tokens_show_empty_is_error():
    from typer.testing import CliRunner

    from navig_text.commands.design import design_app

    r = CliRunner().invoke(design_app, ["tokens", "show", "--json"])
    assert r.exit_code == 1
    assert json.loads(r.output.strip().splitlines()[-1])["ok"] is False


def _fake_space(monkeypatch, name: str, root):
    """Point discover_space_paths at a single tmp space named *name*."""
    from types import SimpleNamespace

    from navig.spaces.contracts import normalize_space_name

    import navig.spaces.resolver as resolver

    key = normalize_space_name(name)
    monkeypatch.setattr(resolver, "discover_space_paths", lambda *a, **k: {key: SimpleNamespace(path=root)})


def test_per_space_save_and_load(monkeypatch, tmp_path):
    from navig_text.design_tokens import load_tokens, save_tokens

    space_root = tmp_path / "brand"
    space_root.mkdir()
    _fake_space(monkeypatch, "brand", space_root)

    res = save_tokens(SAMPLE, space="brand")
    assert res["space"] == "brand"
    # Tokens land in the SPACE's .navig/design — not the global data dir.
    assert (space_root / ".navig" / "design" / "tokens.json").exists()
    assert (space_root / ".navig" / "design" / "tokens.css").exists()
    assert load_tokens(space="brand")["colors"] == SAMPLE["colors"]
    assert load_tokens() is None  # global stays empty — spaces are isolated


def test_unknown_space_raises(monkeypatch):
    from navig_text.design_tokens import save_tokens

    monkeypatch.setattr("navig.spaces.resolver.discover_space_paths", lambda *a, **k: {})
    with pytest.raises(ValueError):
        save_tokens(SAMPLE, space="ghost")


def test_cli_tokens_space_roundtrip(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from navig_text.commands.design import design_app

    space_root = tmp_path / "brand"
    space_root.mkdir()
    _fake_space(monkeypatch, "brand", space_root)

    payload = base64.b64encode(json.dumps(SAMPLE).encode()).decode()
    r = CliRunner().invoke(design_app, ["tokens", "save", "--b64", payload, "--space", "brand"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output.strip().splitlines()[-1])["space"] == "brand"

    r2 = CliRunner().invoke(design_app, ["tokens", "show", "--space", "brand", "--json"])
    assert r2.exit_code == 0, r2.output
    assert json.loads(r2.output.strip().splitlines()[-1])["tokens"]["colors"] == SAMPLE["colors"]


def test_cli_tokens_show_unknown_space(monkeypatch):
    from typer.testing import CliRunner

    from navig_text.commands.design import design_app

    monkeypatch.setattr("navig.spaces.resolver.discover_space_paths", lambda *a, **k: {})
    r = CliRunner().invoke(design_app, ["tokens", "show", "--space", "ghost", "--json"])
    assert r.exit_code == 2
    assert "not found" in json.loads(r.output.strip().splitlines()[-1])["error"]


# ── data-loss / honesty: atomic write + surface a corrupt file ───────────────


def test_save_is_atomic_preserves_old_on_replace_failure(tmp_path, monkeypatch):
    """A write that fails mid-replace must leave the PREVIOUS design system intact,
    never a truncated/empty tokens.json. A plain in-place write_text would overwrite
    the file before any replace — atomic write goes to a temp file that is discarded
    on failure, so the old set survives."""
    from navig_text.design_tokens import load_tokens, save_tokens

    save_tokens(SAMPLE)  # v1 lands atomically

    import navig.core.yaml_io as yaml_io

    def _boom(*_a, **_k):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(yaml_io, "_atomic_replace", _boom)
    with pytest.raises(OSError):
        save_tokens({**SAMPLE, "colors": ["#abcabc"]})

    # The old, complete v1 is still readable — no data loss, no truncation.
    assert load_tokens()["colors"] == SAMPLE["colors"]
    # …and no stray temp file was left behind in the design dir.
    d = tmp_path / "design"
    assert sorted(p.name for p in d.iterdir()) == ["tokens.css", "tokens.json"]


def test_load_surfaces_corrupt_file(tmp_path):
    """A present-but-unparseable tokens.json must RAISE (honest), not return None —
    masking it as 'none saved' would tell the user to re-extract over a real design
    system still on disk (the phantom-empty class)."""
    from navig_text.design_tokens import load_tokens, save_tokens

    save_tokens(SAMPLE)
    (tmp_path / "design" / "tokens.json").write_text("{ not valid json", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt|unreadable"):
        load_tokens()


def test_cli_tokens_show_corrupt_is_honest(tmp_path):
    """`navig design tokens show` over a corrupt file reports it honestly (exit 2),
    NOT the exit-1 'no tokens saved yet' path that a phantom-empty would produce."""
    from typer.testing import CliRunner

    from navig_text.commands.design import design_app

    payload = base64.b64encode(json.dumps(SAMPLE).encode()).decode()
    CliRunner().invoke(design_app, ["tokens", "save", "--b64", payload])
    (tmp_path / "design" / "tokens.json").write_text("{ corrupt", encoding="utf-8")

    r = CliRunner().invoke(design_app, ["tokens", "show", "--json"])
    assert r.exit_code == 2, r.output  # honest error, not exit-1 "no tokens saved"
    err = json.loads(r.output.strip().splitlines()[-1])["error"]
    assert "corrupt" in err or "unreadable" in err
