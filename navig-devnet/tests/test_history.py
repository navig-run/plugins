"""Per-space repeater history — record / load / get round-trip, isolated to a
tmp dir so it never touches a real space's .navig."""

from __future__ import annotations

import pytest

from navig_devnet import history


@pytest.fixture
def hist_dir(tmp_path, monkeypatch):
    d = tmp_path / ".navig" / "devnet"
    d.mkdir(parents=True)
    monkeypatch.setattr(history, "_history_dir", lambda: d)
    return d


def _result(method="GET", url="http://x/", status=200, body="hello"):
    return {
        "request": {"method": method, "url": url, "headers": [], "body_len": 0},
        "response": {
            "status": status, "reason": "OK", "headers": [["content-type", "text/plain"]],
            "content_type": "text/plain", "body": body, "body_len": len(body),
            "body_truncated": False, "final_url": url,
        },
        "redirects": [], "elapsed_ms": 12.3,
    }


def test_record_assigns_increasing_ids(hist_dir):
    assert history.record(_result(url="http://a/")) == 1
    assert history.record(_result(url="http://b/")) == 2
    rows = history.load()
    assert [r["id"] for r in rows] == [1, 2]
    assert rows[-1]["url"] == "http://b/"


def test_get_by_id(hist_dir):
    history.record(_result(url="http://a/", body="AAA"))
    history.record(_result(url="http://b/", status=404, body="nope"))
    e = history.get(2)
    assert e is not None and e["status"] == 404 and e["response_body"] == "nope"
    assert history.get(99) is None


def test_load_limit_and_corrupt_line_skipped(hist_dir):
    for i in range(5):
        history.record(_result(url=f"http://{i}/"))
    (hist_dir / "http_history.jsonl").open("a", encoding="utf-8").write("{ not json\n")
    rows = history.load(limit=3)
    assert len(rows) == 3  # newest 3, corrupt line skipped, no raise
    assert rows[-1]["url"] == "http://4/"


def test_record_never_raises_on_failure(monkeypatch):
    # An unwritable history dir returns None, never propagates (the send already happened).
    monkeypatch.setattr(history, "_history_dir", lambda: (_ for _ in ()).throw(OSError("ro")))
    assert history.record(_result()) is None
