"""Assembly-line orchestration — sequencing + graceful skip (no live plugins).

The capability probes and stage calls are monkeypatched, so the test verifies
the *wiring logic* (which stages run, which skip, and that nothing fails
silently) without touching real download/LLM/TTS/publish code.
"""

from __future__ import annotations

import asyncio

import pytest

from navig_pipeline import pipeline as P


@pytest.fixture
def set_caps(monkeypatch):
    def _set(**overrides):
        base = {"acquire": True, "transcribe": True, "script": True, "narrate": True, "publish": True}
        base.update(overrides)
        monkeypatch.setattr(P, "detect_capabilities", lambda: base)
    return _set


@pytest.fixture(autouse=True)
def stub_stages(monkeypatch):
    async def _script(topic, *, kind, out_dir):
        return f"Draft[{kind}]: {topic}"

    async def _narrate(script, *, out_dir):
        return f"{out_dir}/voice.mp3"

    async def _publish(caption, *, url, to, campaign, dry_run):
        if dry_run:
            return [f"preview:{p}" for p in to]
        return [type("R", (), {"ok": True})() for _ in to]

    async def _transcribe(path):
        return "transcribed words"

    async def _acquire(url, *, out_dir):
        return f"{out_dir}/downloaded.mp4"

    monkeypatch.setattr(P, "_do_script", _script)
    monkeypatch.setattr(P, "_do_narrate", _narrate)
    monkeypatch.setattr(P, "_do_publish", _publish)
    monkeypatch.setattr(P, "_do_transcribe", _transcribe)
    monkeypatch.setattr(P, "_do_acquire", _acquire)


def _run(**kw):
    return asyncio.run(P.run_pipeline(**kw))


def test_topic_dryrun_drafts_and_previews(set_caps, tmp_path):
    set_caps()
    rep = _run(topic="ship it", to=["x", "telegram"], dry_run=True, work_dir=tmp_path)
    status = {s.name: s.status for s in rep.stages}
    assert rep.caption == "Draft[social]: ship it"
    assert status["script"] == "ran"
    assert status["publish"] == "preview"
    assert len(rep.receipts) == 2


def test_missing_social_skips_publish_not_silently(set_caps, tmp_path):
    set_caps(publish=False)
    rep = _run(topic="x", to=["x"], dry_run=True, work_dir=tmp_path)
    pub = next(s for s in rep.stages if s.name == "publish")
    assert pub.status == "skipped" and "not installed" in pub.detail
    assert rep.receipts == []


def test_missing_text_aborts_with_reason(set_caps, tmp_path):
    set_caps(script=False)
    rep = _run(topic="x", to=["x"], dry_run=True, work_dir=tmp_path)
    scr = next(s for s in rep.stages if s.name == "script")
    assert scr.status == "skipped" and rep.caption is None


def test_narrate_dryrun_is_a_reported_skip(set_caps, tmp_path):
    set_caps()
    rep = _run(topic="x", to=["x"], narrate=True, dry_run=True, work_dir=tmp_path)
    nar = next(s for s in rep.stages if s.name == "narrate")
    assert nar.status == "skipped" and "dry-run" in nar.detail


def test_narrate_live_runs_and_records_path(set_caps, tmp_path):
    set_caps()
    rep = _run(topic="x", to=["x"], narrate=True, dry_run=False, work_dir=tmp_path)
    nar = next(s for s in rep.stages if s.name == "narrate")
    assert nar.status == "ran"
    assert rep.narration_path.endswith("voice.mp3")


def test_no_topic_no_source_is_an_error(set_caps, tmp_path):
    set_caps()
    rep = _run(to=["x"], dry_run=True, work_dir=tmp_path)
    scr = next(s for s in rep.stages if s.name == "script")
    assert scr.status == "failed"


# ── acquire: URL sources actually download then transcribe ──────────────────


def test_url_source_live_downloads_then_transcribes(set_caps, tmp_path):
    set_caps()
    rep = _run(source="https://example.com/clip.mp4", to=["x"], dry_run=False, work_dir=tmp_path)
    status = {s.name: s.status for s in rep.stages}
    assert status["acquire"] == "ran"
    assert status["transcribe"] == "ran"
    assert status["script"] == "ran"  # seeded from the transcript
    assert rep.source_path.endswith("downloaded.mp4")


def test_url_source_dryrun_skips_download_and_transcribe(set_caps, tmp_path):
    set_caps()
    rep = _run(source="https://example.com/clip.mp4", to=["x"], dry_run=True, work_dir=tmp_path)
    acq = next(s for s in rep.stages if s.name == "acquire")
    tr = next(s for s in rep.stages if s.name == "transcribe")
    assert acq.status == "skipped" and "dry-run" in acq.detail
    assert tr.status == "skipped"
    assert rep.caption is not None  # still drafts (falls back to summarizing the URL)


def test_url_source_without_download_plugin_skips_acquire(set_caps, tmp_path):
    set_caps(acquire=False)
    rep = _run(source="https://x/clip.mp4", to=["x"], dry_run=False, work_dir=tmp_path)
    acq = next(s for s in rep.stages if s.name == "acquire")
    assert acq.status == "skipped" and "not installed" in acq.detail
    assert rep.source_path is None


# ── honest reporting: failures never read as success ────────────────────────


def test_publish_all_fail_marks_stage_failed(set_caps, tmp_path, monkeypatch):
    set_caps()

    class _Fail:
        ok = False
        network = "x"
        error = "no token"

    async def _pub_fail(caption, *, url, to, campaign, dry_run):
        return [_Fail() for _ in to]

    monkeypatch.setattr(P, "_do_publish", _pub_fail)
    rep = _run(topic="t", to=["x", "telegram"], dry_run=False, work_dir=tmp_path)
    pub = next(s for s in rep.stages if s.name == "publish")
    assert pub.status == "failed" and "no token" in pub.detail


def test_transcribe_none_marks_failed_not_zero_chars(set_caps, tmp_path, monkeypatch):
    set_caps()

    async def _tr_none(path):
        return None

    monkeypatch.setattr(P, "_do_transcribe", _tr_none)
    rep = _run(source="https://x/clip.mp4", to=["x"], dry_run=False, work_dir=tmp_path)
    tr = next(s for s in rep.stages if s.name == "transcribe")
    assert tr.status == "failed" and "empty transcript" in tr.detail
    # script still runs — falls back to summarizing the source URL
    assert next(s for s in rep.stages if s.name == "script").status == "ran"


def test_empty_draft_does_not_publish_placeholder(set_caps, tmp_path, monkeypatch):
    set_caps()

    async def _blank(topic, *, kind, out_dir):
        return "   "

    monkeypatch.setattr(P, "_do_script", _blank)
    rep = _run(topic="t", to=["x"], dry_run=False, work_dir=tmp_path)
    scr = next(s for s in rep.stages if s.name == "script")
    assert scr.status == "failed" and rep.caption is None
    # publish must not have run at all
    assert not any(s.name == "publish" for s in rep.stages)


# ── autopilot: command composition (no gateway needed) ──────────────────────


def test_compose_run_command_is_quote_safe_and_live():
    cmd = P.compose_run_command(
        topic='ship "v2" fast', to=["x", "telegram"], campaign="launch", url="https://navig.run"
    )
    assert cmd.startswith("navig pipeline run ")
    assert "--topic 'ship \"v2\" fast'" in cmd  # shlex.quote → single-quoted, inner quotes literal
    assert "--to x,telegram" in cmd
    assert cmd.endswith("--live")  # the scheduled run publishes
    assert "--kind" not in cmd  # default social omitted


def test_compose_run_command_roundtrips_through_shlex():
    import shlex

    # A trailing backslash broke the old hand-rolled quoting (scheduler uses shlex.split).
    cmd = P.compose_run_command(topic="promo\\", to=["x"])
    tokens = shlex.split(cmd)  # must not raise ValueError: No closing quotation
    assert tokens[:3] == ["navig", "pipeline", "run"]
    assert tokens[tokens.index("--topic") + 1] == "promo\\"  # exact value recovered
    assert tokens[-1] == "--live"


def test_compose_run_command_includes_optionals():
    cmd = P.compose_run_command(topic="t", to=["devto"], kind="article", narrate=True)
    assert "--kind article" in cmd
    assert "--narrate" in cmd


def test_autopilot_job_name_slugifies_and_prefixes():
    assert P.autopilot_job_name(topic="NAVIG v2 ships today!") == "autopilot-navig-v2-ships-today"
    assert P.autopilot_job_name(name="custom") == "custom"
    assert P.autopilot_job_name() == "autopilot-content"
