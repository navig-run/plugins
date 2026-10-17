"""``navig text channel data`` — loud fetch, disk cache, never silently stale.

Network is stubbed at the transport (`_get`), so these run offline: what is pinned
is the CONTRACT — live answers write the cache, a failure falls back to it and says
``_source: cache``, ``--strict`` refuses instead, and no cache + failure is an error.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from navig_text.channel import data as data_mod
from navig_text.channel.data import DataError, fetch
from navig_text.cli import app as standalone_app

runner = CliRunner()

SUMMARY = {
    "pioneers": 41,
    "ignited": 2,
    "mayors": 1,
    "citiesOpen": 266,
    "citiesTotal": 268,
    "seatsToMayor": 8,
    "wave": {
        "number": 1,
        "title": "First Wave",
        "endsAt": "2030-01-01T00:00:00Z",
    },
}
BOARD = {
    "zones": [
        {"slug": "montpellier", "name": "Montpellier", "verified": 7, "toMayor": 8},
        {"slug": "lyon", "name": "Lyon", "verified": 12, "toMayor": 3},
        {"slug": "lille", "name": "Lille", "verified": 40, "toMayor": 5, "ignitedAt": "x"},
    ]
}


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    (tmp_path / "channel.json").write_text(
        json.dumps(
            {
                "data": {
                    "api": "https://api.test",
                    "endpoints": ["/summary", "/board"],
                    "cache": ".live-cache.json",
                }
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def _stub_success(monkeypatch: pytest.MonkeyPatch):
    def fake_get(url: str, timeout: int) -> dict:
        return SUMMARY if url.endswith("/summary") else BOARD

    monkeypatch.setattr(data_mod, "_get", fake_get)


def _stub_failure(monkeypatch: pytest.MonkeyPatch):
    def fake_get(url: str, timeout: int) -> dict:
        raise OSError("network down")

    monkeypatch.setattr(data_mod, "_get", fake_get)


# ── live path ─────────────────────────────────────────────────────────────────
def test_live_fetch_shapes_and_caches(ws: Path, monkeypatch: pytest.MonkeyPatch):
    _stub_success(monkeypatch)
    data = fetch(ws)
    assert data.source == "live"
    assert data["pioneers"] == 41
    assert data["citiesOpen"] == 266
    assert data["founderNext"] == 42
    assert data["citiesDark"] == 266
    # lead = the un-ignited zone furthest along (lille is ignited → excluded)
    assert data["lead"]["name"] == "Lyon"
    assert data["mpl"]["name"] == "Montpellier"
    assert data["doorDate"] == "1 January"
    cache = json.loads((ws / ".live-cache.json").read_text(encoding="utf-8"))
    assert cache["_source"] == "live"
    assert cache["pioneers"] == 41


def test_days_left_counts_down(ws: Path, monkeypatch: pytest.MonkeyPatch):
    _stub_success(monkeypatch)
    data = fetch(ws)
    assert data["daysLeft"] is None or data["daysLeft"] >= 0


# ── failure paths ─────────────────────────────────────────────────────────────
def test_failure_falls_back_to_cache_and_says_so(
    ws: Path, monkeypatch: pytest.MonkeyPatch
):
    _stub_success(monkeypatch)
    fetch(ws)  # seed the cache
    _stub_failure(monkeypatch)
    data = fetch(ws, strict=False)
    assert data.source == "cache"
    assert data["_ageHours"] >= 0
    assert data["pioneers"] == 41  # last good answer


def test_strict_refuses_cache_fallback(ws: Path, monkeypatch: pytest.MonkeyPatch):
    _stub_success(monkeypatch)
    fetch(ws)
    _stub_failure(monkeypatch)
    with pytest.raises(DataError) as exc:
        fetch(ws, strict=True)
    assert "--strict" in str(exc.value)


def test_failure_without_cache_raises(ws: Path, monkeypatch: pytest.MonkeyPatch):
    _stub_failure(monkeypatch)
    with pytest.raises(DataError) as exc:
        fetch(ws, strict=False)
    assert "no cache" in str(exc.value)


def test_shape_from_workspace_module(ws: Path, monkeypatch: pytest.MonkeyPatch):
    """channel.json can point the shaping at the workspace's own function."""
    (ws / "mydata.py").write_text(
        "def shape(summary, board):\n"
        "    return {'pioneers': summary['pioneers'], 'via': 'workspace'}\n",
        encoding="utf-8",
        newline="\n",
    )
    cfg = json.loads((ws / "channel.json").read_text(encoding="utf-8"))
    cfg["data"]["shape"] = "mydata:shape"
    (ws / "channel.json").write_text(json.dumps(cfg), encoding="utf-8")
    _stub_success(monkeypatch)
    data = fetch(ws)
    assert data["via"] == "workspace"
    assert data["pioneers"] == 41


def test_bad_shape_spec_is_a_dataerror(ws: Path):
    cfg = json.loads((ws / "channel.json").read_text(encoding="utf-8"))
    cfg["data"]["shape"] = "nocolon"
    (ws / "channel.json").write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(DataError):
        fetch(ws)


# ── CLI ───────────────────────────────────────────────────────────────────────
def test_cli_data_json(ws: Path, monkeypatch: pytest.MonkeyPatch):
    _stub_success(monkeypatch)
    result = runner.invoke(
        standalone_app, ["channel", "data", "--workspace", str(ws), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["pioneers"] == 41


def test_cli_data_failure_exits_1(ws: Path, monkeypatch: pytest.MonkeyPatch):
    _stub_failure(monkeypatch)
    result = runner.invoke(
        standalone_app, ["channel", "data", "--workspace", str(ws)]
    )
    assert result.exit_code == 1, result.output


def test_cli_data_table_shows_cache_warning(
    ws: Path, monkeypatch: pytest.MonkeyPatch
):
    _stub_success(monkeypatch)
    runner.invoke(standalone_app, ["channel", "data", "--workspace", str(ws)])
    _stub_failure(monkeypatch)
    result = runner.invoke(
        standalone_app, ["channel", "data", "--workspace", str(ws)]
    )
    assert result.exit_code == 0, result.output
    assert "Cached" in result.output or "stale" in result.output
