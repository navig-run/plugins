"""Gateway API (/api/deck/games/*) — route mounting + handler JSON (engine mocked)."""

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from navig_games.deck_routes import games as gr


def _run(coro):
    return asyncio.run(coro)


def _body(resp) -> dict:
    return json.loads(resp.body)


def _ajson(payload):
    async def _j():
        return payload
    return _j


@pytest.fixture(autouse=True)
def _reset_claim_state():
    """The claim job keeps module-level state; reset it around every test."""
    gr._claim_lock.update({
        "running": False, "started_at": None, "finished_at": None,
        "dry_run": False, "game": None, "ok": None, "summary": None,
    })
    gr._claim_task = None
    yield


def test_register_mounts_all_routes():
    app = web.Application()
    gr.register(app)
    paths = {r.resource.canonical for r in app.router.routes()}
    assert paths == {
        "/api/deck/games/status", "/api/deck/games/free",
        "/api/deck/games/deals", "/api/deck/games/library",
        "/api/deck/games/history", "/api/deck/games/unify",
        "/api/deck/games/claim", "/api/deck/games/claim/status",
        "/api/deck/games/login/capture", "/api/deck/games/schedule",
        "/api/deck/games/deals/watchlist", "/api/deck/games/deals/watch",
        "/api/deck/games/grab",
    }


class _Game:
    launchable = True
    key = "gog:x"
    store = "gog"
    launch_exe = "C:/g/x.exe"
    title = "X"

    def to_dict(self):
        return {"store": "gog", "title": "X"}


def _patch_unify(monkeypatch, add_result):
    from navig_games.engine import library
    from navig_games.engine.steam import grid, shortcuts

    monkeypatch.setattr(library, "scan_non_steam", lambda stores=None: [_Game()])
    monkeypatch.setattr(shortcuts, "present_exes", lambda id3=None: set())
    monkeypatch.setattr(grid, "apply_for_game", lambda g, **k: {"ok": True, "set": []})
    monkeypatch.setattr(grid, "icon_path_for", lambda g, id3=None: "")
    monkeypatch.setattr(shortcuts, "add_games", lambda games, **k: add_result)


def test_handle_unify_success(monkeypatch):
    _patch_unify(monkeypatch, {"ok": True, "added": ["X"], "skipped_present": [],
                               "skipped_unlaunchable": []})
    resp = _run(gr.handle_unify(make_mocked_request("POST", "/api/deck/games/unify")))
    body = _body(resp)
    assert resp.status == 200 and body["ok"] and body["data"]["added"] == ["X"]


def test_handle_unify_steam_running_is_409(monkeypatch):
    _patch_unify(monkeypatch, {"ok": False, "error": "Steam is running — quit Steam first"})
    resp = _run(gr.handle_unify(make_mocked_request("POST", "/api/deck/games/unify")))
    body = _body(resp)
    assert resp.status == 409 and body["ok"] is False and "running" in body["error"]


def test_handle_unify_nothing_to_add(monkeypatch):
    from navig_games.engine import library

    monkeypatch.setattr(library, "scan_non_steam", lambda stores=None: [])
    resp = _run(gr.handle_unify(make_mocked_request("POST", "/api/deck/games/unify")))
    body = _body(resp)
    assert resp.status == 200 and body["data"]["added"] == []


def test_handle_deals_fresh_bypasses_cache(monkeypatch):
    from navig_games.engine.sources import steam

    captured = {}

    def _fake(threshold=20, use_cache=True):
        captured["use_cache"] = use_cache
        return {"steamid": "1", "checked": 0, "capped": False, "deals": []}

    monkeypatch.setattr(steam, "check_deals", _fake)
    _run(gr.handle_deals(make_mocked_request("GET", "/api/deck/games/deals?fresh=1")))
    assert captured["use_cache"] is False
    _run(gr.handle_deals(make_mocked_request("GET", "/api/deck/games/deals")))
    assert captured["use_cache"] is True


def test_handle_free(monkeypatch):
    import navig_games.engine.runner as runner

    monkeypatch.setattr(runner, "check",
                        lambda store="epic": {"store": "epic",
                                              "current": [{"title": "Nova"}], "upcoming": []})
    resp = _run(gr.handle_free(make_mocked_request("GET", "/api/deck/games/free")))
    body = _body(resp)
    assert resp.status == 200 and body["ok"]
    assert body["data"]["current"][0]["title"] == "Nova"


def test_handle_deals(monkeypatch):
    from navig_games.engine.sources import steam

    class _Deal:
        def to_dict(self):
            return {"name": "Deal", "discount_pct": 75}

    monkeypatch.setattr(steam, "check_deals",
                        lambda threshold=20, use_cache=True: {"steamid": "1", "checked": 2, "capped": False,
                                              "deals": [_Deal()]})
    resp = _run(gr.handle_deals(make_mocked_request("GET", "/api/deck/games/deals?threshold=50")))
    body = _body(resp)
    assert resp.status == 200 and body["ok"]
    assert body["data"]["deals"][0]["discount_pct"] == 75


def test_handle_library(monkeypatch):
    from navig_games.engine import library

    class _Game:
        store = "gog"

        def to_dict(self):
            return {"store": "gog", "title": "SYNTH"}

    monkeypatch.setattr(library, "scan", lambda: [_Game()])
    resp = _run(gr.handle_library(make_mocked_request("GET", "/api/deck/games/library")))
    body = _body(resp)
    assert resp.status == 200 and body["data"]["count"] == 1
    assert body["data"]["games"][0]["store"] == "gog"


def test_handler_error_is_500_not_a_crash(monkeypatch):
    import navig_games.engine.runner as runner

    def _boom(store="epic"):
        raise RuntimeError("sourcing down")

    monkeypatch.setattr(runner, "check", _boom)
    resp = _run(gr.handle_free(make_mocked_request("GET", "/api/deck/games/free")))
    body = _body(resp)
    assert resp.status == 500 and body["ok"] is False
    assert "sourcing down" in body["error"]


def test_handle_status(monkeypatch):
    from navig_games.engine import library

    class _Game:
        store = "steam"

        def to_dict(self):
            return {}

    monkeypatch.setattr(library, "scan", lambda: [_Game()])
    resp = _run(gr.handle_status(make_mocked_request("GET", "/api/deck/games/status")))
    body = _body(resp)
    assert resp.status == 200 and body["ok"]
    d = body["data"]
    assert "version" in d and d["installed"]["count"] == 1
    assert set(d["schedules"]) == {"claim", "deals", "unify"}


# --- history + claim job -------------------------------------------------------


def test_handle_history_newest_first(monkeypatch):
    from navig_games.engine.ledger import Ledger

    recs = [
        {"title": "Old", "store": "epic", "status": "claimed", "updated": "2026-07-01T00:00:00"},
        {"title": "New", "store": "epic", "status": "needs_manual", "updated": "2026-07-10T00:00:00"},
    ]
    monkeypatch.setattr(Ledger, "all", lambda self: list(recs))
    resp = _run(gr.handle_history(make_mocked_request("GET", "/api/deck/games/history")))
    body = _body(resp)
    assert resp.status == 200 and body["data"]["count"] == 2
    assert body["data"]["records"][0]["title"] == "New"  # newest first


def _parsed_claim_json():
    return b'{"claimed": 1, "results": [{"title": "Nova", "status": "claimed", "message": "ok"}]}'


class _FakeProc:
    def __init__(self, out: bytes, rc: int = 0):
        self._out = out
        self.returncode = rc
        self.pid = 4242

    async def communicate(self):
        return (self._out, b"")


def _mock_exec(monkeypatch, proc):
    captured: dict = {"args": ()}

    async def _fake(*args, **kwargs):
        captured["args"] = args
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake)
    return captured


def _settle(pred, limit=200):
    async def _wait():
        for _ in range(limit):
            if pred():
                return
            await asyncio.sleep(0)

    _run(_wait())


def test_handle_claim_starts_and_records_summary(monkeypatch):
    cap = _mock_exec(monkeypatch, _FakeProc(_parsed_claim_json()))

    async def _drive():
        resp = await gr.handle_claim(make_mocked_request("POST", "/api/deck/games/claim"))
        assert resp.status == 200 and _body(resp)["data"]["running"] is True
        task = gr._claim_task
        if task:
            await task

    _run(_drive())
    assert "--yes" in cap["args"] and "--dry-run" not in cap["args"]  # real claim, not dry-run
    assert gr._claim_lock["running"] is False
    assert gr._claim_lock["ok"] is True
    assert gr._claim_lock["summary"]["claimed"] == 1
    assert gr._claim_lock["summary"]["results"][0]["title"] == "Nova"


def test_run_claim_dry_run_passes_flag(monkeypatch):
    cap = _mock_exec(monkeypatch, _FakeProc(_parsed_claim_json()))
    _run(gr._run_claim(dry_run=True))
    assert "--dry-run" in cap["args"] and "--yes" not in cap["args"]


def test_handle_claim_when_running_is_409(monkeypatch):
    gr._claim_lock["running"] = True
    resp = _run(gr.handle_claim(make_mocked_request("POST", "/api/deck/games/claim")))
    body = _body(resp)
    assert resp.status == 409 and body["ok"] is False and "running" in body["error"]


def test_handle_claim_status_shape():
    resp = _run(gr.handle_claim_status(make_mocked_request("GET", "/api/deck/games/claim/status")))
    body = _body(resp)
    assert resp.status == 200 and body["ok"]
    assert set(body["data"]) == {"running", "started_at", "finished_at", "dry_run", "game", "ok", "summary"}


def test_parse_claim_output_falls_back_on_garbage():
    out = gr._parse_claim_output(b"not json\nrandom log line", b"boom on stderr", 1)
    assert out["claimed"] == 0 and out["error"] == "exit 1"
    assert "boom on stderr" in out["message"]


def test_parse_claim_output_handles_pretty_printed_json():
    # `navig games claim --json` prints a *pretty-printed* (multi-line) object —
    # the whole of stdout is the JSON, so the parser must not read it line-by-line.
    out = (
        b'{\n  "store": "epic",\n  "claimed": 0,\n  "results": [\n'
        b'    {"title": "Nova Lands", "status": "already_owned", "message": "already in your library"},\n'
        b'    {"title": "Tattoo Tycoon", "status": "already_owned", "message": "already in your library"}\n'
        b"  ]\n}"
    )
    s = gr._parse_claim_output(out, b"", 0)
    assert s["claimed"] == 0 and s["error"] is None
    assert [r["title"] for r in s["results"]] == ["Nova Lands", "Tattoo Tycoon"]
    assert s["results"][0]["status"] == "already_owned"


def test_parse_claim_output_tolerates_wrapping_log_lines():
    out = b'2026-07-11 INFO booting\n{"claimed": 1, "results": []}\ndone.'
    s = gr._parse_claim_output(out, b"", 0)
    assert s["claimed"] == 1


def test_parse_claim_output_passes_login_mechanism_through():
    out = b'{"claimed": 2, "login": "session_restored", "results": []}'
    s = gr._parse_claim_output(out, b"", 0)
    assert s["login"] == "session_restored" and s["claimed"] == 2
    # absent login → None (never KeyErrors)
    s2 = gr._parse_claim_output(b'{"claimed": 0, "results": []}', b"", 0)
    assert s2["login"] is None


# --- schedule (automations toggle) ---------------------------------------------


class _FakeJob:
    def __init__(self, jid, name):
        self.id = jid
        self.name = name


class _FakeCron:
    def __init__(self, jobs=None):
        self._jobs = list(jobs or [])
        self.added, self.removed, self.enabled_ids = [], [], []

    def list_jobs(self):
        return self._jobs

    def add_job(self, name, schedule, command):
        self.added.append((name, schedule, command))
        j = _FakeJob(f"job_{len(self._jobs) + 1}", name)
        self._jobs.append(j)
        return j

    def enable_job(self, jid):
        self.enabled_ids.append(jid)
        return True

    def remove_job(self, jid):
        self.removed.append(jid)
        self._jobs = [j for j in self._jobs if j.id != jid]
        return True


def _patch_live(monkeypatch, svc):
    import navig.scheduler.cron_service as cs

    monkeypatch.setattr(cs, "get_live_service", lambda: svc)


def test_apply_schedule_live_enable_adds(monkeypatch):
    from navig_games.engine import schedule as sched

    fake = _FakeCron()
    _patch_live(monkeypatch, fake)
    live = gr._apply_schedule_live(sched.CLAIM_JOB, sched.CLAIM_COMMAND, "daily", True, "claim", sched)
    assert live is True and fake.added and fake.added[0][0] == sched.CLAIM_JOB


def test_apply_schedule_live_enable_existing_reenables(monkeypatch):
    from navig_games.engine import schedule as sched

    fake = _FakeCron([_FakeJob("job_1", sched.CLAIM_JOB)])
    _patch_live(monkeypatch, fake)
    gr._apply_schedule_live(sched.CLAIM_JOB, sched.CLAIM_COMMAND, "daily", True, "claim", sched)
    assert fake.enabled_ids == ["job_1"] and not fake.added  # re-enabled, not duplicated


def test_apply_schedule_live_disable_removes(monkeypatch):
    from navig_games.engine import schedule as sched

    fake = _FakeCron([_FakeJob("job_1", sched.CLAIM_JOB)])
    _patch_live(monkeypatch, fake)
    gr._apply_schedule_live(sched.CLAIM_JOB, sched.CLAIM_COMMAND, "daily", False, "claim", sched)
    assert fake.removed == ["job_1"]


def test_apply_schedule_live_file_fallback(monkeypatch):
    from navig_games.engine import schedule as sched

    _patch_live(monkeypatch, None)  # no live service → file path
    called = {}
    monkeypatch.setattr(sched, "enable_deals", lambda when="daily": called.setdefault("when", when))
    live = gr._apply_schedule_live(sched.DEALS_JOB, sched.DEALS_COMMAND, "daily", True, "deals", sched)
    assert live is False and called.get("when") == "daily"


def test_handle_schedule_unknown_job_is_400():
    resp = _run(gr.handle_schedule(make_mocked_request("POST", "/api/deck/games/schedule")))
    body = _body(resp)
    assert resp.status == 400 and body["ok"] is False and "unknown" in body["error"]


# --- login capture -------------------------------------------------------------


def test_handle_login_capture_signed_in(monkeypatch):
    _mock_exec(monkeypatch, _FakeProc(b'{"ok": true, "name": "PlayerOne"}'))
    resp = _run(gr.handle_login_capture(make_mocked_request("POST", "/api/deck/games/login/capture")))
    body = _body(resp)
    assert resp.status == 200 and body["ok"]
    assert body["data"]["ok"] is True and body["data"]["name"] == "PlayerOne"


def test_handle_login_capture_opened_login(monkeypatch):
    _mock_exec(monkeypatch, _FakeProc(b'{"ok": false, "opened": true}'))
    resp = _run(gr.handle_login_capture(make_mocked_request("POST", "/api/deck/games/login/capture")))
    body = _body(resp)
    assert resp.status == 200 and body["data"]["ok"] is False and body["data"]["opened"] is True


def test_handle_login_capture_409_when_claiming(monkeypatch):
    gr._claim_lock["running"] = True
    resp = _run(gr.handle_login_capture(make_mocked_request("POST", "/api/deck/games/login/capture")))
    body = _body(resp)
    assert resp.status == 409 and body["ok"] is False


def test_json_from_output():
    assert gr._json_from_output(b'{"a": 1}')["a"] == 1
    assert gr._json_from_output(b'log\n{\n  "a": 2\n}\ntail')["a"] == 2
    assert gr._json_from_output(b"not json at all") is None
    assert gr._json_from_output(b"") is None


# --- epic sign-in detection (the real root-cause fix) --------------------------


def test_epic_session_present_matches_domain(monkeypatch):
    from navig.vault import sessions as S
    from navig_games.engine.claim import epic

    # get_session(domain) misses username-labelled sessions; list_sessions matches.
    monkeypatch.setattr(
        S, "list_sessions",
        lambda: [{"domain": "epicgames.com", "username": "nevahudo", "label": "web-session/epicgames.com/nevahudo"}],
    )
    assert epic.epic_session_present() == (True, "nevahudo")

    monkeypatch.setattr(S, "list_sessions", lambda: [{"domain": "store.steampowered.com"}])
    assert epic.epic_session_present() == (False, None)

    monkeypatch.setattr(S, "list_sessions", lambda: [])
    assert epic.epic_session_present() == (False, None)


def test_status_reports_epic_account(monkeypatch):
    from navig_games.engine import library
    from navig_games.engine.claim import epic

    monkeypatch.setattr(library, "scan", lambda: [])
    monkeypatch.setattr(epic, "epic_session_present", lambda: (True, "PlayerOne"))
    resp = _run(gr.handle_status(make_mocked_request("GET", "/api/deck/games/status")))
    d = _body(resp)["data"]
    assert d["epic_signed_in"] is True and d["epic_account"] == "PlayerOne"


# --- last_run (claim health rollup) --------------------------------------------


def test_last_run_record_and_read(monkeypatch, tmp_path):
    from navig_games.engine import last_run

    monkeypatch.setattr(last_run, "_path", lambda: tmp_path / "last_run.json")
    assert last_run.read() is None
    last_run.record({
        "checked": 2, "attempted": 2, "claimed": 1, "login": "session_restored",
        "results": [{"status": "claimed"}, {"status": "needs_manual"}], "dry_run": False,
    })
    lr = last_run.read()
    assert lr["claimed"] == 1 and lr["needs_manual"] == 1 and lr["login"] == "session_restored"
    assert lr["finished_at"]


def test_last_run_ignores_dry_run(monkeypatch, tmp_path):
    from navig_games.engine import last_run

    monkeypatch.setattr(last_run, "_path", lambda: tmp_path / "last_run.json")
    last_run.record({"claimed": 1, "dry_run": False, "results": []})
    last_run.record({"claimed": 9, "dry_run": True, "results": []})  # a test — must not overwrite
    assert last_run.read()["claimed"] == 1


def test_last_run_read_survives_corrupt_file(monkeypatch, tmp_path):
    from navig_games.engine import last_run

    p = tmp_path / "last_run.json"
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(last_run, "_path", lambda: p)
    assert last_run.read() is None  # never raises


def test_status_includes_last_run(monkeypatch):
    from navig_games.engine import last_run, library

    monkeypatch.setattr(library, "scan", lambda: [])
    monkeypatch.setattr(last_run, "read", lambda: {"claimed": 2, "finished_at": "2026-07-12T00:00:00Z"})
    resp = _run(gr.handle_status(make_mocked_request("GET", "/api/deck/games/status")))
    d = _body(resp)["data"]
    assert d["last_run"]["claimed"] == 2


# --- per-game claim + Free-tab claim_status enrich -----------------------------


def test_run_claim_per_game_passes_game_flag(monkeypatch):
    cap = _mock_exec(monkeypatch, _FakeProc(_parsed_claim_json()))
    _run(gr._run_claim(dry_run=False, game="epic:nova"))
    assert "--game" in cap["args"] and "epic:nova" in cap["args"]
    assert "--all" not in cap["args"]  # per-game, not bulk


def test_run_claim_bulk_uses_all_not_game(monkeypatch):
    cap = _mock_exec(monkeypatch, _FakeProc(_parsed_claim_json()))
    _run(gr._run_claim(dry_run=False, game=None))
    assert "--all" in cap["args"] and "--game" not in cap["args"]


def test_check_enriches_current_with_claim_status(monkeypatch):
    from navig_games.engine import runner
    from navig_games.engine.ledger import Ledger
    from navig_games.engine.models import FreeGame

    g = FreeGame(store="epic", title="Nova Lands", slug="nova-lands")
    monkeypatch.setattr(
        "navig_games.engine.sources.epic.fetch_free_games",
        lambda country=None, locale=None: {"current": [g], "upcoming": []},
    )
    monkeypatch.setattr(Ledger, "get", lambda self, k: {"status": "already_owned"} if k == g.key else None)

    out = runner.check("epic")
    row = out["current"][0]
    assert row["key"] == g.key and row["claim_status"] == "already_owned"


# --- deals watchlist (watch / unwatch / list) ----------------------------------


def test_handle_watch_add_and_remove(monkeypatch):
    from navig_games.engine.sources import steam

    store = {"wl": []}
    monkeypatch.setattr(steam, "add_watch", lambda a: store["wl"].append(a) or True)
    monkeypatch.setattr(steam, "remove_watch", lambda a: (a in store["wl"]) and (store["wl"].remove(a) or True))
    monkeypatch.setattr(steam, "get_watchlist", lambda: list(store["wl"]))

    # add via a store URL → parsed to appid 1245620
    req = make_mocked_request("POST", "/api/deck/games/deals/watch")
    monkeypatch.setattr(req, "json", _ajson({"appid": "https://store.steampowered.com/app/1245620/X/", "watch": True}))
    monkeypatch.setattr(type(req), "can_read_body", property(lambda self: True))
    resp = _run(gr.handle_watch(req))
    body = _body(resp)
    assert resp.status == 200 and body["data"]["appid"] == 1245620
    assert body["data"]["changed"] is True and body["data"]["watchlist"] == [1245620]


def test_handle_watch_bad_appid_is_400(monkeypatch):
    req = make_mocked_request("POST", "/api/deck/games/deals/watch")
    monkeypatch.setattr(req, "json", _ajson({"appid": "not-a-game"}))
    monkeypatch.setattr(type(req), "can_read_body", property(lambda self: True))
    resp = _run(gr.handle_watch(req))
    assert resp.status == 400 and _body(resp)["ok"] is False


def test_handle_watchlist_returns_named(monkeypatch):
    from navig_games.engine.sources import steam

    monkeypatch.setattr(steam, "watchlist_named",
                        lambda: [{"appid": 1245620, "name": "Elden Ring", "url": "u"}])
    resp = _run(gr.handle_watchlist(make_mocked_request("GET", "/api/deck/games/deals/watchlist")))
    body = _body(resp)
    assert resp.status == 200 and body["data"]["count"] == 1
    assert body["data"]["watches"][0]["name"] == "Elden Ring"


# ── grab: the terminal state for games we can't claim ────────────────────────


def _grab_req(payload):
    req = make_mocked_request("POST", "/api/deck/games/grab")
    req.__dict__["json"] = _ajson(payload)
    type(req).can_read_body = property(lambda self: True)
    return req


def test_grab_marks_a_game(monkeypatch):
    calls = {}

    def _mark(key, *, grabbed=True):
        calls.update(key=key, grabbed=grabbed)
        return {"key": key, "title": "NIGHTBELL", "grabbed": grabbed, "changed": True}

    monkeypatch.setattr("navig_games.engine.runner.mark_grabbed", _mark)
    resp = _run(gr.handle_grab(_grab_req({"game": "itch:3697"})))

    assert resp.status == 200
    assert calls == {"key": "itch:3697", "grabbed": True}
    assert _body(resp)["data"]["title"] == "NIGHTBELL"


def test_grab_undo_passes_the_flag_through(monkeypatch):
    calls = {}

    def _mark(key, *, grabbed=True):
        calls.update(key=key, grabbed=grabbed)
        return {"key": key, "grabbed": grabbed, "changed": True}

    monkeypatch.setattr("navig_games.engine.runner.mark_grabbed", _mark)
    resp = _run(gr.handle_grab(_grab_req({"game": "itch:3697", "grabbed": False})))

    assert resp.status == 200
    assert calls["grabbed"] is False


def test_grab_without_a_key_is_a_400():
    resp = _run(gr.handle_grab(_grab_req({})))
    assert resp.status == 400


def test_grab_of_an_expired_game_is_a_404(monkeypatch):
    monkeypatch.setattr("navig_games.engine.runner.mark_grabbed",
                        lambda key, *, grabbed=True: {"error": "that game isn't free right now"})
    resp = _run(gr.handle_grab(_grab_req({"game": "itch:gone"})))
    assert resp.status == 404


def test_grab_cannot_rewrite_claim_history(monkeypatch):
    """The engine's refusal surfaces as a 409, not a silent success."""
    monkeypatch.setattr(
        "navig_games.engine.runner.mark_grabbed",
        lambda key, *, grabbed=True: {
            "error": "that game was claimed or already owned — history, not a note"},
    )
    resp = _run(gr.handle_grab(_grab_req({"game": "epic:x", "grabbed": False})))
    assert resp.status == 409
