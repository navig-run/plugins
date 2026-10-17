"""`npm run land` adapter — real milestones in, git-verified result out.

``scripts/ci-land.mjs`` appends one JSON line per milestone to the file named by
``NAVIG_LAND_EVENTS`` (a no-op when unset). The runner tails that file and turns
the lines into stage events, so progress is the land script's own state, never a
guess from its console text.

Line schema (all keys optional except ``stage``)::

    {"stage": "gate", "state": "running|done|failed|skipped", "detail": "attempt 2/5",
     "meta": {"branch": "...", "worktree": "...", "sha": "<full landed sha>",
              "base": "<sha it was gated on>", "outcome": "landed|nothing|dry-run"}}

A final ``{"stage": "result", ...}`` line carries the outcome. The land script
exiting 0 is NOT trusted on its own: for a real land the adapter re-checks git.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

#: The fixed milestone list of a land — what makes "n of N" honest for this run kind.
STAGES = ["preflight", "queue", "rebase", "gate", "push", "close-pr", "delete-branch"]

_SHA = re.compile(r"[0-9a-f]{7,40}\Z")


def _git(cwd: Path, *args: str, timeout: float = 60.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout or proc.stderr or "").strip()


def context(cwd: Path) -> dict[str, str]:
    """Branch + worktree slug of the checkout `land` will run in (for the run label)."""
    rc, branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD", timeout=10)
    rc2, top = _git(cwd, "rev-parse", "--show-toplevel", timeout=10)
    worktree = ""
    if rc2 == 0:
        parts = Path(top).parts
        if "worktrees" in parts:
            idx = len(parts) - 1 - parts[::-1].index("worktrees")
            if idx + 1 < len(parts):
                worktree = parts[idx + 1]
    return {"branch": branch if rc == 0 else "", "worktree": worktree,
            "project": Path(top).name if rc2 == 0 and not worktree else (Path(top).parents[2].name if worktree else "")}


def read_new_lines(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    """Parse JSON lines appended after *offset*; returns (events, new_offset). Partial lines wait."""
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            chunk = fh.read()
    except OSError:
        return [], offset
    if not chunk:
        return [], offset
    end = chunk.rfind(b"\n")
    if end < 0:
        return [], offset
    out: list[dict[str, Any]] = []
    for line in chunk[: end + 1].decode("utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("stage"), str):
            out.append(obj)
    return out, offset + end + 1


def verify(cwd: Path, branch: str, sha: str) -> tuple[bool, str]:
    """Did the land really happen? The landed commit must be on origin/main and the branch gone."""
    if not _SHA.match(sha or ""):
        return False, "land reported success but no landed commit was recorded"
    rc, out = _git(cwd, "fetch", "origin", "main", "-q", timeout=90)
    if rc != 0:
        return False, f"could not fetch origin/main to verify: {out[:120]}"
    rc, _ = _git(cwd, "merge-base", "--is-ancestor", sha, "origin/main", timeout=30)
    if rc != 0:
        return False, f"landed commit {sha[:9]} is not on origin/main"
    if branch:
        rc, out = _git(cwd, "ls-remote", "--heads", "origin", branch, timeout=60)
        if rc == 0 and out.strip():
            return True, f"{sha[:9]} on origin/main; remote branch {branch} still exists"
    return True, f"{sha[:9]} verified on origin/main"
