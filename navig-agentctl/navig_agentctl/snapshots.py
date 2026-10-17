"""Read-only snapshots: sessions, worktrees, tasks.

Every function here DERIVES state from machinery that already exists —
``navig procs`` (processes.py), ``navig repo`` (repo.py), BoardStore and
work.db — and normalises it to the Control Center contract. Nothing here
executes, launches, kills, or writes to those stores; the only writes are to
:mod:`navig_agentctl.registry` (observation upserts).

Contract shapes (the fixture both navig-dev and the OS app validate against):

sessions[]  {session_id, pid, project_root, project, worktree, agent_kind,
             editor, state, reason, last_activity_at, owner, mem_mb}
worktrees[] {path, name, branch, head, is_primary, is_sibling, locked,
             ahead, behind, dirty, last_commit:{sha, subject, at}}
tasks[]     {ref, source, title, status, owner, deps, worktree, ref_type,
             ref_id, updated_at, gate, priority}
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

# ── sessions ─────────────────────────────────────────────────────────────────


def session_snapshot(idle_hours: float = 24.0) -> list[dict[str, Any]]:
    """Live agent sessions, classified exactly like ``navig procs``.

    Reuses processes.py wholesale (describe + classify) so the Control Center
    and the CLI can never disagree about what "idle" means.
    """
    from navig.commands.processes import (
        classify_claude_sessions,
        describe_processes,
        find_processes_by_name,
    )

    rows = describe_processes(find_processes_by_name(["claude"]))
    rows = [r for r in rows if (r.get("name") or "").lower().removesuffix(".exe") == "claude"]
    rows = classify_claude_sessions(rows, idle_hours)

    out: list[dict[str, Any]] = []
    for r in rows:
        project = r.get("project") or ""
        out.append(
            {
                "session_id": r.get("session_id") or f"pid:{r.get('pid')}",
                "pid": r.get("pid"),
                "project_root": r.get("cwd") or "",
                "project": project,
                "worktree": project.split(":", 1)[1] if ":" in project else "",
                "agent_kind": "claude-code",
                "editor": _editor_of(r.get("owner") or ""),
                "state": r.get("verdict") or "unknown",
                "reason": r.get("reason") or "",
                "last_activity_at": None,  # transcript mtime flows through idle_h only
                "owner": r.get("owner") or "",
                "mem_mb": r.get("mem_mb") or 0.0,
            }
        )
    return out


def _editor_of(owner: str) -> str:
    """'code[123]' → 'vscode'; 'cursor[…]' → 'cursor'; else '' (CLI)."""
    name = owner.split("[", 1)[0].lower()
    if "code" in name or "codium" in name:
        return "vscode"
    if "cursor" in name:
        return "cursor"
    return ""


def record_snapshot(rows: list[dict[str, Any]], store: Any) -> int:
    """Persist an observation into the registry. Returns rows actually recorded
    (a row with no session_id is skipped, not counted)."""
    n = 0
    for row in rows:
        try:
            if store.upsert_session(row):
                n += 1
        except Exception:  # noqa: BLE001 — one bad row must not lose the snapshot
            continue
    return n


# ── worktrees ────────────────────────────────────────────────────────────────


def worktree_snapshot(root: Path) -> list[dict[str, Any]]:
    """Every git worktree of `root`, with ahead/behind + dirty + last commit.

    Wraps ``navig.commands.repo.list_worktrees`` (the sanctioned parser) and
    adds the read-only git facts the board shows. Never mutates anything.

    The per-worktree git calls (dirty · ahead/behind · last commit) run in a
    thread pool: 41 worktrees × 3 serial subprocesses measured 1m47s on the
    live gateway — unusable in a UI. Threads (not processes) because every
    call is a subprocess wait; row order stays the input order.
    """
    from concurrent.futures import ThreadPoolExecutor

    from navig.commands.repo import default_branch, dirty_ref, list_worktrees

    wts = list_worktrees(root)
    if not wts:
        return []
    base = default_branch(root) or "main"

    def _one(wt: dict) -> dict[str, Any]:
        path: Path = wt["path"]
        sha, is_dirty = dirty_ref(path)
        ahead, behind = _ahead_behind(path, base, wt.get("branch"))
        return {
            "path": str(path),
            "name": wt.get("label") or path.name,
            "branch": wt.get("branch") or "",
            "head": sha or wt.get("head") or "",
            "is_primary": bool(wt.get("is_primary")),
            "is_sibling": bool(wt.get("is_sibling")),
            "locked": wt.get("locked") or "",
            "ahead": ahead,
            "behind": behind,
            "dirty": bool(is_dirty),
            "last_commit": _last_commit(path),
        }

    workers = min(8, max(2, len(wts)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_one, wts))


def _git(path: Path, *args: str) -> str:
    try:
        res = subprocess.run(
            ["git", *args],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )
        return res.stdout.strip() if res.returncode == 0 else ""
    except Exception:  # noqa: BLE001 — git absent/locked: report empty, never fail
        return ""


def _ahead_behind(path: Path, base: str, branch: str | None) -> tuple[int, int]:
    if not branch:
        return 0, 0
    raw = _git(path, "rev-list", "--left-right", "--count", f"{base}...{branch}")
    parts = raw.split()
    if len(parts) != 2:
        return 0, 0
    try:
        return int(parts[1]), int(parts[0])  # left=base-only=behind, right=branch-only=ahead
    except ValueError:
        return 0, 0


def _last_commit(path: Path) -> dict[str, Any]:
    raw = _git(path, "log", "-1", "--format=%H%x1f%s%x1f%aI")
    if not raw:
        return {"sha": "", "subject": "", "at": ""}
    sha, subject, at = (raw.split("\x1f") + ["", ""])[:3]
    return {"sha": sha[:12], "subject": subject[:120], "at": at}


def conflict_pairs(root: Path) -> list[dict[str, Any]]:
    """Pairwise merge-conflict radar (read-only `git merge-tree` simulation).

    Same pair rows as ``repo.collect_conflicts``, but the pair simulations fan
    out across a thread pool: 42 worktrees = 861 pairs measured **673 s
    serial** on the live box — no UI timeout survives that. Opt-in at the
    route (``?conflicts=1``); callers budget for tens of seconds, not minutes.
    """
    from concurrent.futures import ThreadPoolExecutor
    from itertools import combinations

    from navig.commands.repo import dirty_ref, list_worktrees, pair_conflicts

    try:
        wts = list_worktrees(root)
        cand = [wt for wt in wts if wt.get("branch") or wt.get("head")]
        if not cand:
            return []

        def _side(wt: dict) -> dict[str, Any]:
            ref = wt.get("branch") or wt.get("head")
            entry: dict[str, Any] = {**wt, "ref": ref, "dirty": False}
            sha, is_dirty = dirty_ref(wt["path"])
            entry["dirty"] = is_dirty
            if sha:
                entry["ref"] = sha  # branch tip + uncommitted tracked changes
            return entry

        with ThreadPoolExecutor(max_workers=min(8, max(2, len(cand)))) as pool:
            sides = list(pool.map(_side, cand))

        todo = list(combinations(sides, 2))

        def _pair(ab: tuple[dict, dict]) -> dict[str, Any]:
            a, b = ab
            result = pair_conflicts(root, a["ref"], b["ref"])
            return {
                "a": a.get("label") or a["path"].name,
                "b": b.get("label") or b["path"].name,
                "branch_a": (a.get("branch") or "detached") + (" +dirty" if a["dirty"] else ""),
                "branch_b": (b.get("branch") or "detached") + (" +dirty" if b["dirty"] else ""),
                **result,
            }

        if not todo:
            return []
        workers = min(16, max(4, len(todo)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(_pair, todo))
    except Exception:  # noqa: BLE001 — radar is best-effort telemetry
        return []


# ── tasks (unified read model) ───────────────────────────────────────────────


def task_snapshot(*, limit: int = 200) -> list[dict[str, Any]]:
    """Board cards + work items, one normalised row per task.

    Both stores are read-only here. `ref` is "board:<id>" / "work:<slug>" —
    the binding key the registry uses.
    """
    out: list[dict[str, Any]] = []
    out.extend(_board_tasks())
    out.extend(_work_tasks())
    out.sort(key=lambda t: t.get("updated_at") or "", reverse=True)
    return out[:limit]


def _board_tasks() -> list[dict[str, Any]]:
    try:
        from navig.store.board import get_board_store

        cards = get_board_store().list_cards()
    except Exception:  # noqa: BLE001 — board may not exist yet
        return []
    out = []
    for c in cards:
        out.append(
            {
                "ref": f"board:{c['id']}",
                "source": "board",
                "title": c.get("title") or "",
                "status": c.get("stage") or "",
                "owner": c.get("agent_status") or "",
                "deps": [f"board:{d}" for d in (c.get("deps") or [])],
                "worktree": "",
                "ref_type": "mission" if c.get("mission_id") else "",
                "ref_id": c.get("mission_id") or "",
                "updated_at": c.get("updated_at") or "",
                "gate": c.get("gate") or "",
                "priority": c.get("priority") or "normal",
            }
        )
    return out


def _work_tasks() -> list[dict[str, Any]]:
    try:
        rows = _work_rows()
    except Exception:  # noqa: BLE001 — work.db may not exist yet
        return []
    out = []
    for r in rows:
        out.append(
            {
                "ref": f"work:{r['slug']}",
                "source": "work",
                "title": r["title"],
                "status": r["stage"],
                "owner": r["owner"] or "",
                "deps": _work_deps(r),
                "worktree": "",
                "ref_type": r["ref_type"] or "",
                "ref_id": r["ref_id"] or "",
                "updated_at": r["updated_at"] or "",
                "gate": "",
                "priority": "normal",
            }
        )
    return out


def _work_rows() -> list[dict[str, Any]]:
    """Read work_items read-only from work.db (path matches commands/work.py)."""
    import sqlite3

    from navig.platform.paths import config_dir

    db = config_dir() / "store" / "work.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        rows = conn.execute(
            "SELECT slug, title, stage, owner, ref_type, ref_id, tags_json, metadata_json, updated_at "
            "FROM work_items WHERE stage != 'archived' ORDER BY updated_at DESC LIMIT 200"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _work_deps(row: dict[str, Any]) -> list[str]:
    """depends-on lives in the record file's frontmatter; metadata_json is the hook.

    Phase 1: deps arrive via metadata_json["depends_on"] when a record set them;
    the tracked record file remains the scope source of truth.
    """
    import json

    meta = row.get("metadata_json") or "{}"
    try:
        data = json.loads(meta) if isinstance(meta, str) else (meta or {})
    except Exception:  # noqa: BLE001 — malformed metadata must not blank the row
        return []
    deps = data.get("depends_on") or []
    return [f"work:{d}" for d in deps if isinstance(d, str)]
