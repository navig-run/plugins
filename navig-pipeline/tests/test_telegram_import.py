"""The archive-pipeline subprocess must be bounded: a wedged ingest.py can't hang the
import forever, and a slow/failed pipeline never leads to a delete. (telegram_import's
first tests — `_run_pipeline` is the shell-out that `run_import` now offloads + bounds.)"""
from __future__ import annotations

import subprocess

from navig_pipeline import telegram_import as ti


def test_run_pipeline_times_out_returns_124_and_never_deletes(monkeypatch):
    monkeypatch.setattr(ti, "_resolve_ingest", lambda: "ingest.py")

    def _raise_timeout(*_a, **kw):
        raise subprocess.TimeoutExpired(cmd="ingest", timeout=kw.get("timeout", 0))

    monkeypatch.setattr(ti.subprocess, "run", _raise_timeout)
    rc, log = ti._run_pipeline("/stage", skip_ocr=False, timeout=0.01)
    assert rc == 124  # non-zero → run_import returns pipeline_failed → NO delete
    assert "exceeded" in log and "nothing deleted" in log


def test_run_pipeline_missing_ingest_is_a_clear_failure(monkeypatch):
    monkeypatch.setattr(ti, "_resolve_ingest", lambda: None)
    rc, log = ti._run_pipeline("/stage", skip_ocr=False)
    assert rc == 1 and "not found" in log


def test_run_pipeline_passes_through_returncode_and_output(monkeypatch):
    monkeypatch.setattr(ti, "_resolve_ingest", lambda: "ingest.py")

    class _Proc:
        returncode = 0
        stdout = "routed 12 files"
        stderr = ""

    captured = {}

    def _run(argv, **kw):
        captured["timeout"] = kw.get("timeout")
        captured["skip_ocr"] = "--skip-ocr" in argv
        return _Proc()

    monkeypatch.setattr(ti.subprocess, "run", _run)
    rc, log = ti._run_pipeline("/stage", skip_ocr=True)
    assert rc == 0 and log == "routed 12 files"
    assert captured["timeout"] is not None and captured["timeout"] > 0  # always bounded
    assert captured["skip_ocr"] is True


def test_pipeline_timeout_config(monkeypatch):
    monkeypatch.delenv("NAVIG_ARCHIVE_TIMEOUT", raising=False)
    assert ti._pipeline_timeout() == 3600.0                 # default
    monkeypatch.setenv("NAVIG_ARCHIVE_TIMEOUT", "600")
    assert ti._pipeline_timeout() == 600.0                  # override
    monkeypatch.setenv("NAVIG_ARCHIVE_TIMEOUT", "not-a-number")
    assert ti._pipeline_timeout() == 3600.0                 # bad value → safe default
