# Changelog — navig-agentctl

## 0.4.2 — 2026-10-17

- **Fixed** `navig agentctl runs` printed "N failed/unacknowledged" counting interrupted
  runs — the same mislabel as the Dock counter (fixed in 0.4.1); interrupted runs are now
  named as their own count.

## 0.4.1 — 2026-10-17

- **Fixed** three honesty defects found in live use, measured after one day:
  (a) the Dock printed "N failed" counting interrupted runs and never-observed background
  launches (62 phantom failures in 24 h) — `failed_unacked` now counts only `failed`;
  (b) a stall was claimed from silence alone, so sessions idling between tool calls were
  flagged — a stall is now only ever claimed while the run's own process is verified
  **alive**, an unprovable claim is retracted, and its detail refreshes to the real idle
  time instead of freezing at the first flag;
  (c) a hook-only launch whose completion was never observed closes as `ended` (unknown
  finish), not `interrupted` (died).

## 0.4.0 — 2026-10-16

- **Added** the **Agent Monitor** — a run ledger for Claude Code sessions, subagents, long
  commands and `npm run land`. `POST /api/deck/agents/events` (idempotent, all-or-nothing) and
  `GET /api/deck/agents/runs[/summary|/{id}]` + `POST …/{id}/ack`; schema v2 adds the `run` and
  `run_event` tables (additive migration). A reaper marks stalled runs and vanished PIDs
  (`interrupted`, never guessed `failed`) and drains events a producer spooled while the daemon
  was down.
- **Added** a fail-open Claude Code hook adapter (`python -m navig_agentctl.hook`) mapping the
  documented lifecycle hooks; `Stop` is a turn end, never a run end; background Bash is
  `launched` (completion unknown). `navig agentctl hooks install|status|uninstall` edits Claude
  Code settings diff-first and never touches other hooks.
- **Added** `navig agentctl run -- <cmd>`: real PID/exit code, redacted log, `--verify`,
  `--stall-after`, `--detach`; `npm run land` is recognised and its milestones are read from the
  land script's own event file, with git verification of the result.
- **Added** `navig agentctl runs|show|ack`; the Control Center OS tile gains a **Runs** tab and
  the Dock gains a live chip.
- **Fixed** a spool drain past its 2000-event cap kept only that batch and deleted the rest; the
  remainder now stays claimed for the next pass (every event, in order, exactly once).
- **Security** secrets are redacted on ingest, in log files, and again on read; a run's log is
  only served from the monitor's own log directory (a client-supplied path can never read an
  arbitrary file).

## 0.3.0 — 2026-10-14

- **Added** Phase 2b — the task-binding ledger: `GET/POST /api/deck/agents/bindings` +
  `DELETE /api/deck/agents/bindings/{task_ref}` (one active claim per task ref; rebind moves
  the owner; release stamps `released_at` and the ledger survives for `?all=1` history). CLI:
  `navig agentctl bindings|bind|unbind`. Control Center shows per-task `claimed/unclaimed`
  chips and `owns <task>` lines on session rows. Malformed refs/ids fail closed (400, never a
  partial write); a down store answers 500, never a phantom-empty list.
- **Changed** heartbeat 400 rejections log a warning (the `_reject` helper, shared with
  bindings) — a silently-retried bad payload can no longer hide.

## 0.2.0 — 2026-10-14

- **Added** Phase 2a — `POST /api/deck/agents/heartbeat`: the extension annotates workspace /
  editor / roster context into the session registry. Daemon-owned `state`, `pid`, `reason` and
  `last_seen_at` are never written (a heartbeating dead session still ages into `stale`);
  malformed bodies are 400, never a partial write. The fixture records the mounted contract in
  the same change.

## 0.1.0 — 2026-10-13

- **Added** Agent Control Center, Phase 1 (Observer): `navig agentctl sessions|worktrees|tasks|status`,
  deck routes `GET /api/deck/agents/{sessions,worktrees,tasks}`, and the `agentctl` module tile.
- **Added** `AgentCtlStore` (`agentctl.db`): persistent session ledger with state-change events,
  plus task-ownership bindings (read-only in Phase 1).
- Read-only by design: wraps `navig procs` / `navig repo` / BoardStore / work.db readers; never
  launches, kills, merges or deploys.
