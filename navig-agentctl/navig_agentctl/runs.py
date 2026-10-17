"""Run ledger — the Agent Monitor's persistent execution records.

A *run* is one observed unit of agent work: a Claude Code session, a subagent, a
Bash command, a NAVIG-managed process, or an ``npm run land``. Every producer
(the Claude Code hook, ``agentctl run``, the land adapter, a remote node) speaks
the same small event vocabulary and :meth:`RunsMixin.apply_event` is the only
writer, so state transitions are decided in exactly one place.

Honesty rules baked in here:

* A terminal state (``succeeded`` / ``failed`` / ``interrupted`` / ``ended``) is
  final — late or replayed events can append history but never resurrect a run.
* ``Stop`` (a conversation turn finishing) is a ``turn_end`` event: it flips the
  session's *phase* to ``idle`` and nothing else. Only a process exit code or an
  explicit ``end`` ever completes a run.
* Progress is *declared*: ``stages_total`` exists only when a producer announced
  its stage list. Nothing here computes a percentage or an ETA.
* A run whose process vanished without reporting is ``interrupted`` (not
  ``failed``): we know it stopped, not that it errored.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from navig.store.base import utcnow
from navig_agentctl.redact import redact

KINDS = ("session", "subagent", "bash", "navig", "land")
LIVE_STATES = ("running", "stalled", "needs_input", "launched")
TERMINAL_STATES = ("succeeded", "failed", "interrupted", "ended")
END_STATES = TERMINAL_STATES  # the values an ``end`` event may carry
STAGE_STATES = ("pending", "running", "done", "failed", "skipped")
PHASES = ("working", "idle", "waiting")

#: Seconds without activity before a *working* run is flagged ``stalled``.
DEFAULT_STALL_S = {"session": 900, "subagent": 900, "bash": 600, "navig": 900, "land": 1800}
#: A hook-only background launch whose end was never observed is closed after this.
LAUNCHED_EXPIRES_S = 6 * 3600
#: A finished conversation turn only earns a notification if it ran at least this long.
TURN_NOTIFY_MIN_S = 60
KEEP_RUN_EVENTS = 40
KEEP_TERMINAL_RUNS = 500
KEEP_TERMINAL_DAYS = 30

RUN_DDL = """
CREATE TABLE IF NOT EXISTS run (
    run_id          TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL DEFAULT '',
    parent_run_id   TEXT NOT NULL DEFAULT '',
    kind            TEXT NOT NULL DEFAULT 'bash',
    project         TEXT NOT NULL DEFAULT '',
    worktree        TEXT NOT NULL DEFAULT '',
    node            TEXT NOT NULL DEFAULT '',
    label           TEXT NOT NULL DEFAULT '',
    command         TEXT NOT NULL DEFAULT '',
    pid             INTEGER,
    pid_create_time REAL,
    state           TEXT NOT NULL DEFAULT 'running',
    phase           TEXT NOT NULL DEFAULT 'working',
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    exit_code       INTEGER,
    last_activity_at TEXT NOT NULL,
    log_path        TEXT NOT NULL DEFAULT '',
    stages_json     TEXT NOT NULL DEFAULT '[]',
    stages_total    INTEGER,
    verified        INTEGER,
    detail          TEXT NOT NULL DEFAULT '',
    stall_after_s   INTEGER NOT NULL DEFAULT 0,
    acked_at        TEXT,
    turn_started_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_run_state ON run(state);
CREATE INDEX IF NOT EXISTS ix_run_project ON run(project);
CREATE INDEX IF NOT EXISTS ix_run_started ON run(started_at);
CREATE TABLE IF NOT EXISTS run_event (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    event_id    TEXT UNIQUE,
    at          TEXT NOT NULL,
    type        TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_run_event_run ON run_event(run_id, id);
"""

_TEXT_LIMITS = {"label": 160, "command": 400, "detail": 300, "project": 120, "worktree": 200,
                "node": 80, "log_path": 400}


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None


def _ago(seconds: float) -> str:
    """'45s' under two minutes, else whole minutes — never a misleading '0 min'."""
    return f"{int(seconds)}s" if seconds < 120 else f"{int(seconds // 60)} min"


def _txt(value: Any, key: str) -> str:
    return redact(value, limit=_TEXT_LIMITS.get(key, 200)) if value else ""


def _safe_log(value: Any) -> str:
    """A log path is only ever stored when it sits inside the monitor's log directory."""
    from navig_agentctl.logs import safe_log_path

    path = safe_log_path(value)
    return str(path) if path else ""


def is_valid_id(value: object, *, limit: int = 160) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and all(ord(c) >= 32 for c in value)
    )


def public_run(row: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """The API/CLI shape of a run row: parsed stages, derived elapsed, no raw internals."""
    now = now or datetime.now(timezone.utc)
    try:
        stages = json.loads(row.get("stages_json") or "[]")
    except ValueError:
        stages = []
    started = _parse(row.get("started_at"))
    ended = _parse(row.get("ended_at"))
    last = _parse(row.get("last_activity_at"))
    end_ref = ended or now
    return {
        "run_id": row["run_id"],
        "session_id": row["session_id"],
        "parent_run_id": row["parent_run_id"],
        "kind": row["kind"],
        "project": row["project"],
        "worktree": row["worktree"],
        "node": row["node"],
        "label": row["label"] or row["command"][:60],
        "command": row["command"],
        "pid": row["pid"],
        "state": row["state"],
        "phase": row["phase"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "elapsed_s": int((end_ref - started).total_seconds()) if started else None,
        "idle_s": int((now - last).total_seconds()) if last and not ended else None,
        "last_activity_at": row["last_activity_at"],
        "exit_code": row["exit_code"],
        "verified": None if row["verified"] is None else bool(row["verified"]),
        "detail": row["detail"],
        "stages": stages,
        "stages_done": sum(1 for s in stages if s.get("state") == "done"),
        # None = the producer never declared a total; callers must not invent one.
        "stages_total": row["stages_total"],
        "has_log": bool(row["log_path"]),
        "acked": bool(row["acked_at"]),
    }


class RunsMixin:
    """Mixed into :class:`navig_agentctl.registry.AgentCtlStore`."""

    # ── writes ─────────────────────────────────────────────────────────────

    def apply_event(self, ev: dict[str, Any]) -> dict[str, Any]:
        """Apply one producer event. Idempotent on ``event_id``.

        Returns ``{"run": public_run | None, "duplicate": bool, "transition": dict | None}``.
        ``transition`` is set only when the change deserves a notification:
        ``{"kind": "done"|"failed"|"needs_input"|"stalled", "from": ..., "to": ...}``.
        """
        rid = ev.get("run_id")
        etype = str(ev.get("type") or "")
        if not is_valid_id(rid):
            raise ValueError("run_id is required (non-empty string, ≤160 chars)")
        if etype not in ("start", "activity", "stage", "needs_input", "input_resolved", "turn_end", "end"):
            raise ValueError(f"unknown event type: {etype!r}")
        now = utcnow()
        event_id = ev.get("event_id")
        if event_id is not None and not is_valid_id(event_id):
            raise ValueError("event_id must be a non-empty string of at most 160 chars")

        row = self._read_one("SELECT * FROM run WHERE run_id = ?", (rid,))
        detail = _txt(ev.get("detail"), "detail")
        hist = detail
        if etype == "stage" and isinstance(ev.get("stage"), dict):
            st = ev["stage"]
            hist = _txt(f"{st.get('name', '')}: {st.get('state', 'done')} {st.get('detail', '')}".strip(), "detail")

        if event_id is not None:
            cur = self._write(
                "INSERT OR IGNORE INTO run_event (run_id, event_id, at, type, detail) VALUES (?, ?, ?, ?, ?)",
                (rid, event_id, now, etype, hist),
            )
            if not cur.rowcount:
                return {"run": public_run(dict(row)) if row else None, "duplicate": True, "transition": None}
        else:
            self._write(
                "INSERT INTO run_event (run_id, at, type, detail) VALUES (?, ?, ?, ?)",
                (rid, now, etype, hist),
            )
        self._write(
            "DELETE FROM run_event WHERE run_id = ? AND id NOT IN ("
            "SELECT id FROM run_event WHERE run_id = ? ORDER BY id DESC LIMIT ?)",
            (rid, rid, KEEP_RUN_EVENTS),
        )

        if row is None:
            row = self._create_run(rid, ev, now)
        elif etype == "start":
            self._merge_start(rid, ev)

        cur_state = row["state"]
        if cur_state in TERMINAL_STATES:
            # Final. History was appended above; nothing else may change.
            fresh = self._read_one("SELECT * FROM run WHERE run_id = ?", (rid,))
            return {"run": public_run(dict(fresh)), "duplicate": False, "transition": None}

        transition: dict[str, Any] | None = None
        sets: dict[str, Any] = {}

        if etype in ("activity", "stage", "start"):
            sets["last_activity_at"] = now
            if cur_state == "stalled":
                sets["state"] = "running"
            if cur_state == "needs_input" and etype != "start":
                sets["state"] = "running"
            new_phase = ev["phase"] if ev.get("phase") in PHASES else (
                "working" if etype in ("activity", "stage") else None)
            if new_phase and new_phase != row["phase"]:
                sets["phase"] = new_phase
                if new_phase == "working":
                    sets["turn_started_at"] = now
            if etype != "start" and ev.get("stall_after_s") is not None:
                sets["stall_after_s"] = max(0, int(ev["stall_after_s"]))
        if etype == "stage":
            self._apply_stage(rid, row, ev, sets)
        if etype == "turn_end":
            sets.update(phase="idle", last_activity_at=now)
            if cur_state in ("stalled", "needs_input"):
                sets["state"] = "running"
            if detail:
                sets["detail"] = detail
            started = _parse(row["turn_started_at"])
            took = (_parse(now) - started).total_seconds() if started else 0
            if row["kind"] == "session" and row["phase"] != "idle" and took >= TURN_NOTIFY_MIN_S:
                transition = {"kind": "turn_done", "from": cur_state, "to": cur_state, "took_s": int(took)}
        if etype == "needs_input":
            sets.update(state="needs_input", phase="waiting", last_activity_at=now)
            if detail:
                sets["detail"] = detail
            if cur_state != "needs_input":
                transition = {"kind": "needs_input", "from": cur_state, "to": "needs_input"}
        if etype == "input_resolved" and cur_state == "needs_input":
            sets.update(state="running", phase="working", last_activity_at=now)
        if etype in ("activity", "stage") and cur_state == "needs_input":
            sets["phase"] = "working"
        if etype == "end":
            to_state = ev.get("state") if ev.get("state") in END_STATES else "ended"
            sets.update(state=to_state, ended_at=now, last_activity_at=now, phase="idle")
            if ev.get("exit_code") is not None:
                sets["exit_code"] = int(ev["exit_code"])
            if ev.get("verified") is not None:
                sets["verified"] = 1 if ev["verified"] else 0
            if detail:
                sets["detail"] = detail
            if to_state in ("succeeded", "ended"):
                transition = {"kind": "done", "from": cur_state, "to": to_state}
            elif to_state == "failed":
                transition = {"kind": "failed", "from": cur_state, "to": to_state}
        if etype == "start" and detail:
            sets["detail"] = detail

        self._update_run(rid, sets)
        fresh = self._read_one("SELECT * FROM run WHERE run_id = ?", (rid,))
        return {"run": public_run(dict(fresh)), "duplicate": False, "transition": transition}

    def _create_run(self, rid: str, ev: dict[str, Any], now: str):
        kind = ev.get("kind") if ev.get("kind") in KINDS else "session"
        state = "launched" if ev.get("background") else "running"
        stall = ev.get("stall_after_s")
        stall = DEFAULT_STALL_S.get(kind, 600) if stall is None else max(0, int(stall))
        if state == "launched":
            stall = 0
        planned = ev.get("stages_planned")
        stages = [{"name": _txt(n, "label"), "state": "pending", "at": None} for n in planned][:60] \
            if isinstance(planned, list) else []
        self._write(
            """
            INSERT INTO run (run_id, session_id, parent_run_id, kind, project, worktree, node,
                             label, command, pid, pid_create_time, state, phase, started_at,
                             last_activity_at, log_path, stages_json, stages_total, detail,
                             stall_after_s, turn_started_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'working', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rid,
                str(ev.get("session_id") or "")[:200],
                str(ev.get("parent_run_id") or "")[:160],
                kind,
                _txt(ev.get("project"), "project"),
                _txt(ev.get("worktree"), "worktree"),
                _txt(ev.get("node"), "node"),
                _txt(ev.get("label"), "label"),
                _txt(ev.get("command"), "command"),
                int(ev["pid"]) if ev.get("pid") else None,
                float(ev["pid_create_time"]) if ev.get("pid_create_time") else None,
                state,
                now,
                now,
                _safe_log(ev.get("log_path")),
                json.dumps(stages),
                len(stages) if stages else None,
                _txt(ev.get("detail"), "detail"),
                stall,
                now,
            ),
        )
        return self._read_one("SELECT * FROM run WHERE run_id = ?", (rid,))

    def _merge_start(self, rid: str, ev: dict[str, Any]) -> None:
        """A repeated ``start`` only fills what is still unknown — never overwrites."""
        sets: dict[str, Any] = {}
        cur = self._read_one("SELECT * FROM run WHERE run_id = ?", (rid,))
        for key in ("project", "worktree", "node", "label", "command", "session_id"):
            if not cur[key] and ev.get(key):
                sets[key] = _txt(ev.get(key), key) if key != "session_id" else str(ev[key])[:200]
        if not cur["log_path"] and ev.get("log_path"):
            sets["log_path"] = _safe_log(ev["log_path"])
        if not cur["pid"] and ev.get("pid"):
            sets["pid"] = int(ev["pid"])
            if ev.get("pid_create_time"):
                sets["pid_create_time"] = float(ev["pid_create_time"])
        if isinstance(ev.get("stages_planned"), list) and cur["stages_total"] is None:
            names = [_txt(n, "label") for n in ev["stages_planned"]][:60]
            sets["stages_json"] = json.dumps([{"name": n, "state": "pending", "at": None} for n in names])
            sets["stages_total"] = len(names)
        self._update_run(rid, sets)

    def _apply_stage(self, rid: str, row, ev: dict[str, Any], sets: dict[str, Any]) -> None:
        stage = ev.get("stage") or {}
        name = _txt(stage.get("name"), "label")
        if not name:
            return
        state = stage.get("state") if stage.get("state") in STAGE_STATES else "done"
        try:
            stages = json.loads(row["stages_json"] or "[]")
        except ValueError:
            stages = []
        at = utcnow()
        for s in stages:
            if s.get("name") == name:
                s["state"], s["at"] = state, at
                break
        else:
            if len(stages) < 60:
                stages.append({"name": name, "state": state, "at": at})
        sets["stages_json"] = json.dumps(stages)
        if stage.get("detail"):
            sets["detail"] = _txt(stage["detail"], "detail")

    def _update_run(self, rid: str, sets: dict[str, Any]) -> None:
        if not sets:
            return
        cols = ", ".join(f"{k} = ?" for k in sets)
        self._write(f"UPDATE run SET {cols} WHERE run_id = ?", (*sets.values(), rid))

    def ack_run(self, run_id: str) -> bool:
        cur = self._write(
            "UPDATE run SET acked_at = ? WHERE run_id = ? AND acked_at IS NULL",
            (utcnow(), run_id),
        )
        return bool(cur.rowcount)

    def sweep_runs(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Reaper: stalls, vanished PIDs, expired ``launched`` runs, old history.

        A stall is only ever claimed while the run's own process is verified ALIVE
        (pid-less runs can be silent for hours without anything being wrong, so their
        silence is never evidence); an expired hook-only launch closes as ``ended``
        (unknown finish ≠ failure). Returns notification-worthy transitions as
        ``{"run": public, "kind": ...}``.
        """
        from navig.operation_inflight import pid_is_alive

        now = now or datetime.now(timezone.utc)
        out: list[dict[str, Any]] = []
        for r in self._read_all(
            "SELECT * FROM run WHERE state IN ('running', 'stalled', 'launched', 'needs_input')"
        ):
            row = dict(r)
            last = _parse(row["last_activity_at"]) or now
            idle = (now - last).total_seconds()
            if row["pid"] and not pid_is_alive(row["pid"], row["pid_create_time"]):
                self._finish_swept(row, "interrupted", "process vanished without reporting an exit")
                out.append({"run": self._public(row["run_id"]), "kind": "failed"})
            elif row["state"] == "launched" and idle > LAUNCHED_EXPIRES_S:
                # Never seeing a finisher is not a failure — close neutrally, say so.
                self._finish_swept(row, "ended", "completion was never observed (hook-only launch)")
            elif row["state"] == "stalled":
                # A stall claim must stay provable: refresh it while the process is
                # confirmed alive (pid-dead is handled above), retract one we can no
                # longer back with liveness (pid-less rows — silence is not proof).
                if idle <= row["stall_after_s"] or row["phase"] != "working":
                    sets: dict[str, Any] = {"state": "running"}
                    if str(row["detail"]).startswith("no activity for "):
                        sets["detail"] = ""
                    self._update_run(row["run_id"], sets)
                elif row["pid"]:
                    self._update_run(row["run_id"], {"detail": f"no activity for {_ago(idle)}"})
                else:
                    self._update_run(row["run_id"], {"state": "running", "detail": ""})
            elif (
                row["state"] == "running"
                and row["phase"] == "working"
                and row["stall_after_s"] > 0
                and idle > row["stall_after_s"]
                and row["pid"]  # after the pid-dead check above, a pid here is proven alive
            ):
                self._update_run(row["run_id"], {"state": "stalled", "detail": f"no activity for {_ago(idle)}"})
                self._write(
                    "INSERT INTO run_event (run_id, at, type, detail) VALUES (?, ?, 'stalled', ?)",
                    (row["run_id"], utcnow(), f"idle {int(idle)}s"),
                )
                out.append({"run": self._public(row["run_id"]), "kind": "stalled"})
        self._prune_runs(now)
        return out

    def _finish_swept(self, row: dict[str, Any], state: str, why: str) -> None:
        self._update_run(row["run_id"], {"state": state, "ended_at": utcnow(), "detail": why, "phase": "idle"})
        self._write(
            "INSERT INTO run_event (run_id, at, type, detail) VALUES (?, ?, 'swept', ?)",
            (row["run_id"], utcnow(), why),
        )

    def _prune_runs(self, now: datetime) -> None:
        cutoff = (now - timedelta(days=KEEP_TERMINAL_DAYS)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self._write(
            "DELETE FROM run WHERE state IN ('succeeded','failed','interrupted','ended') AND ended_at < ?",
            (cutoff,),
        )
        self._write(
            "DELETE FROM run WHERE state IN ('succeeded','failed','interrupted','ended') AND run_id NOT IN ("
            "SELECT run_id FROM run WHERE state IN ('succeeded','failed','interrupted','ended') "
            "ORDER BY ended_at DESC LIMIT ?)",
            (KEEP_TERMINAL_RUNS,),
        )
        self._write("DELETE FROM run_event WHERE run_id NOT IN (SELECT run_id FROM run)")

    # ── reads ──────────────────────────────────────────────────────────────

    def _public(self, run_id: str) -> dict[str, Any]:
        return public_run(dict(self._read_one("SELECT * FROM run WHERE run_id = ?", (run_id,))))

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._read_one("SELECT * FROM run WHERE run_id = ?", (run_id,))
        return public_run(dict(row)) if row else None

    def get_run_raw(self, run_id: str) -> dict[str, Any] | None:
        row = self._read_one("SELECT * FROM run WHERE run_id = ?", (run_id,))
        return dict(row) if row else None

    def list_runs(
        self,
        *,
        project: str = "",
        state: str = "",
        kind: str = "",
        active_only: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where, args = [], []
        if project:
            where.append("project LIKE ?")
            args.append(f"%{project}%")
        if state:
            where.append("state = ?")
            args.append(state)
        if kind:
            where.append("kind = ?")
            args.append(kind)
        if active_only:
            where.append("state IN ('running','stalled','needs_input','launched')")
        sql = "SELECT * FROM run"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY (state IN ('running','stalled','needs_input','launched')) DESC, " \
               "COALESCE(ended_at, last_activity_at) DESC LIMIT ?"
        now = datetime.now(timezone.utc)
        return [public_run(dict(r), now=now) for r in self._read_all(sql, (*args, max(1, min(limit, 500))))]

    def run_events(self, run_id: str, limit: int = 40) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._read_all(
                "SELECT at, type, detail FROM run_event WHERE run_id = ? ORDER BY id DESC LIMIT ?",
                (run_id, limit),
            )
        ]

    def runs_summary(self) -> dict[str, Any]:
        """Counts for the compact indicator.

        ``failed_unacked`` counts only state ``failed`` — the Dock prints the number
        next to the word "failed", so interrupted runs (died mid-run, notified once)
        and never-observed launches must not inflate it (measured: 62 phantom
        "failures" after one day of background Bash runs).
        """
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        counts = {s: 0 for s in ("running", "stalled", "needs_input", "launched")}
        for r in self._read_all(
            "SELECT state, COUNT(*) AS n FROM run WHERE state IN ('running','stalled','needs_input','launched') GROUP BY state"
        ):
            counts[r["state"]] = r["n"]
        failed = self._read_one(
            "SELECT COUNT(*) AS n FROM run WHERE state = 'failed' AND acked_at IS NULL AND ended_at > ?",
            (since,),
        )["n"]
        done = self._read_one(
            "SELECT COUNT(*) AS n FROM run WHERE state IN ('succeeded','ended') AND ended_at > ?", (since,)
        )["n"]
        attention = "needs_input" if counts["needs_input"] else "failed" if failed else \
            "stalled" if counts["stalled"] else "running" if counts["running"] else "idle"
        return {**counts, "failed_unacked": failed, "done_24h": done, "attention": attention}


def migrate_runs(conn: sqlite3.Connection) -> None:
    conn.executescript(RUN_DDL)
