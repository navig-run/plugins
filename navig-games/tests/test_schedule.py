"""Generalized scheduler — named jobs for claim + deals, distinct ids, backward-compat."""

import json

from navig.core.json_io import JsonReadError, atomic_write_json, load_json_safe

from navig_games.engine import schedule


def _isolate(tmp_path, monkeypatch):
    jobs = tmp_path / "cron_jobs.json"
    monkeypatch.setattr(schedule, "_jobs_path", lambda: jobs)
    return jobs


def test_claim_and_deals_are_separate_jobs(tmp_path, monkeypatch):
    jobs = _isolate(tmp_path, monkeypatch)

    assert schedule.enable("daily")["action"] == "created"        # claim (back-compat wrapper)
    assert schedule.enable_deals("weekly")["action"] == "created"  # deals

    assert schedule.status()["schedule"] == "daily"
    assert schedule.deals_status()["schedule"] == "weekly"

    data = json.loads(jobs.read_text("utf-8"))
    ids = [j["id"] for j in data["jobs"]]
    assert len(ids) == len(set(ids)) == 2  # unique ids, two jobs
    cmds = {j["name"]: j["command"] for j in data["jobs"]}
    assert cmds[schedule.CLAIM_JOB] == schedule.CLAIM_COMMAND
    assert cmds[schedule.DEALS_JOB] == schedule.DEALS_COMMAND


def test_update_and_remove_are_independent(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    schedule.enable("daily")
    schedule.enable_deals("daily")

    assert schedule.enable("weekly")["action"] == "updated"  # idempotent update
    assert schedule.status()["schedule"] == "weekly"

    schedule.disable()  # remove claim only
    assert not schedule.status()["enabled"]
    assert schedule.deals_status()["enabled"]  # deals untouched


def test_status_empty(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert schedule.status() == {"enabled": False, "schedule": None,
                                 "path": str(tmp_path / "cron_jobs.json")}


def test_three_independent_jobs(tmp_path, monkeypatch):
    jobs = _isolate(tmp_path, monkeypatch)
    schedule.enable("daily")
    schedule.enable_deals("daily")
    schedule.enable_unify("weekly")

    assert schedule.status()["enabled"]
    assert schedule.deals_status()["enabled"]
    assert schedule.unify_status()["schedule"] == "weekly"
    assert schedule.unify_status()["command"] == schedule.UNIFY_COMMAND

    data = json.loads(jobs.read_text("utf-8"))
    assert len({j["id"] for j in data["jobs"]}) == 3  # three distinct jobs

    schedule.disable_unify()
    assert not schedule.unify_status()["enabled"]
    assert schedule.status()["enabled"] and schedule.deals_status()["enabled"]  # others intact


# ── config-wipe guard: cron_jobs.json is SHARED — a locked/corrupt read must not
#    let a games add/remove wipe core backups, habits, or other plugins' schedules ──


def _seed_foreign_job(jobs):
    """A store already holding a NON-games schedule (a core backup)."""
    atomic_write_json(
        {"counter": 5, "jobs": [{
            "id": "job_1", "name": "core: nightly backup", "schedule": "daily",
            "command": "navig backup run", "enabled": True,
        }]},
        jobs,
    )


def _raise_lock(*_a, **_k):
    raise JsonReadError("simulated transient lock (sharing violation)")


def test_upsert_preserves_foreign_schedules(tmp_path, monkeypatch):
    jobs = _isolate(tmp_path, monkeypatch)
    _seed_foreign_job(jobs)

    assert schedule.upsert_job("g: claim", "navig games claim", "daily")["ok"] is True

    names = {j["name"] for j in load_json_safe(jobs, default={})["jobs"]}
    assert names == {"core: nightly backup", "g: claim"}  # the core backup survived the add


def test_transient_lock_refuses_write_no_wipe(tmp_path, monkeypatch):
    jobs = _isolate(tmp_path, monkeypatch)
    _seed_foreign_job(jobs)
    before = jobs.read_bytes()

    monkeypatch.setattr(schedule, "load_json_for_update", _raise_lock)
    res = schedule.upsert_job("g: claim", "navig games claim", "daily")

    assert res["ok"] is False and "unreadable" in res["error"]
    assert jobs.read_bytes() == before  # every foreign schedule survived — nothing wiped


def test_remove_under_lock_refuses_no_wipe(tmp_path, monkeypatch):
    jobs = _isolate(tmp_path, monkeypatch)
    _seed_foreign_job(jobs)
    before = jobs.read_bytes()

    monkeypatch.setattr(schedule, "load_json_for_update", _raise_lock)
    res = schedule.remove_job("core: nightly backup")

    assert res["ok"] is False
    assert jobs.read_bytes() == before  # not removed, not wiped


def test_status_degrades_on_a_corrupt_store(tmp_path, monkeypatch):
    jobs = _isolate(tmp_path, monkeypatch)
    jobs.parent.mkdir(parents=True, exist_ok=True)
    jobs.write_text("{ this is not valid json ,,,", encoding="utf-8")

    st = schedule.job_status("core: nightly backup")  # read-only → must not raise

    assert st["enabled"] is False  # degraded to an empty view
    assert (tmp_path / "cron_jobs.json.corrupt").exists()  # bytes preserved, not silently lost
