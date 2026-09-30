"""Turn what the catalog knows into a reversible move plan.

Two jobs, both plan-first:

* **Class triage** — separate real photographs from the browser-cache litter a
  carve drags in (icons, stock art, UI screenshots), and park the webcam frames
  in their own place.
* **Date recovery** — file photos whose date was *recovered* into the existing
  date tree, but only when the evidence is strong enough to act on.

Nothing is deleted, ever: this library is move-only. Quarantine goes to
``<root>/.trash/<bucket>`` with the relative path preserved, and every applied
run writes a CSV that ``navig explore photos undo`` replays backwards.
"""
from __future__ import annotations

import csv
import shutil
import time
from pathlib import Path

# Below this confidence a derived date is a hint for a human, not grounds to move
# a file. EXIF/overlay/filename clear it; folder-year and sibling-median do not.
MOVE_MIN_CONFIDENCE = 0.80

# Where each non-photograph class is parked. Photographs are never moved by
# triage — deciding where a real photo belongs is the operator's call.
CLASS_DEST = {
    "screenshot": "_screenshots",
    "web-graphic": "_web",
    "document": "_documents",
    "webcam": "_webcam",
}


def build_plan(root: Path, *, date_root: str = "By Date", triage_classes: bool = True,
               refile_dated: bool = False, min_confidence: float = MOVE_MIN_CONFIDENCE,
               min_class_score: float = 0.0) -> tuple[list[dict], dict]:
    """Compute the moves. Pure — touches nothing on disk."""
    from . import catalog  # noqa: PLC0415

    root = Path(root).resolve()
    conn = catalog.connect(root)
    rows: list[dict] = []
    summary: dict = {}

    def bump(k: str) -> None:
        summary[k] = summary.get(k, 0) + 1

    records = conn.execute(
        """SELECT f.path, f.rel, f.name, f.sha256,
                  a.decoded, a.w, a.h,
                  c.class, c.score AS class_score,
                  d.value AS date_value, d.source AS date_source,
                  d.confidence AS date_confidence
           FROM files f
           JOIN assets a ON a.sha256 = f.sha256
           LEFT JOIN classes c ON c.sha256 = f.sha256
           LEFT JOIN dates   d ON d.sha256 = f.sha256
           WHERE f.present=1 AND f.root=?""", (str(root),)).fetchall()

    for r in records:
        src = Path(r["path"])
        rel = r["rel"]

        # Undecodable bytes are a known, named outcome — never a silent skip.
        if not r["decoded"]:
            rows.append({"action": "quarantine-undecodable", "src": str(src),
                         "dst": str(root / ".trash" / "undecodable" / rel),
                         "why": "does not decode as an image"})
            bump("undecodable")
            continue

        cls = r["class"]
        if (triage_classes and cls in CLASS_DEST
                and (r["class_score"] or 0) >= min_class_score):
            dest = root / CLASS_DEST[cls] / Path(rel).name
            if src.parent != dest.parent:
                rows.append({"action": f"triage-{cls}", "src": str(src), "dst": str(dest),
                             "why": f"classified {cls} ({r['class_score']:.3f})"})
                bump(f"triage-{cls}")
                continue

        if refile_dated and r["date_value"] and cls == "photo":
            conf = r["date_confidence"] or 0
            if conf >= min_confidence:
                y = r["date_value"][:4]
                ym = r["date_value"][:7]
                dest = root / date_root / y / ym / Path(rel).name
                if src.parent != dest.parent:
                    rows.append({"action": "refile-dated", "src": str(src), "dst": str(dest),
                                 "why": f"{r['date_value'][:10]} via {r['date_source']} "
                                        f"(confidence {conf:.2f})"})
                    bump("refile-dated")
                    continue
            else:
                bump("date-too-weak-to-move")

        bump("keep")

    return rows, summary


def write_plan(rows: list[dict], out: Path) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["action", "src", "dst", "why"])
        w.writeheader()
        w.writerows(rows)
    return out


def _unique(dst: Path) -> Path:
    """Never overwrite: a collision gets a suffix, so nothing is lost silently."""
    if not dst.exists():
        return dst
    stem, suffix, i = dst.stem, dst.suffix, 1
    while True:
        cand = dst.with_name(f"{stem}__{i}{suffix}")
        if not cand.exists():
            return cand
        i += 1


def _repoint(conn, moves: list[tuple[str, str]]) -> int:
    """Tell the catalog where the files went.

    Without this the catalog keeps pointing at paths this very function just
    emptied, and every downstream command — search, gallery, face crops — hits
    files that are not there any more. Because rows are keyed on content hash,
    updating the path is all that is needed: the dates, faces and person names
    hanging off that hash follow automatically.
    """
    import os  # noqa: PLC0415

    if conn is None or not moves:
        return 0
    n = 0
    for src, dst in moves:
        key = os.path.normcase(os.path.abspath(src))
        new = os.path.normcase(os.path.abspath(dst))
        row = conn.execute("SELECT root FROM files WHERE path=?", (key,)).fetchone()
        if row is None:
            continue
        try:
            rel = Path(dst).resolve().relative_to(Path(row["root"])).as_posix()
        except ValueError:
            rel = Path(dst).name
        conn.execute("UPDATE files SET path=?, rel=?, name=?, present=1 WHERE path=?",
                     (new, rel, Path(dst).name, key))
        n += 1
    conn.commit()
    return n


def apply_plan(rows: list[dict], log_path: Path, *,
               only: set[str] | None = None, conn=None) -> dict:
    """Execute the plan, writing an undo log as it goes.

    Pass ``conn`` (the vision catalog) so the index is repointed at the new
    locations in the same operation that moves them.
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stats: dict = {}
    moves: list[tuple[str, str]] = []
    with log_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["action", "src", "dst"])
        for r in rows:
            if only and r["action"] not in only:
                continue
            src, dst = Path(r["src"]), Path(r["dst"])
            if not src.exists():
                stats["missing"] = stats.get("missing", 0) + 1
                continue
            dst = _unique(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(src), str(dst))
            except OSError:
                stats["failed"] = stats.get("failed", 0) + 1
                continue
            w.writerow([r["action"], str(src), str(dst)])
            moves.append((str(src), str(dst)))
            stats[r["action"]] = stats.get(r["action"], 0) + 1
    if conn is not None:
        stats["catalog_repointed"] = _repoint(conn, moves)
    return stats


def undo(log_path: Path, *, conn=None) -> dict:
    """Replay a triage log backwards, repointing the catalog with it."""
    stats: dict = {}
    moves: list[tuple[str, str]] = []
    with Path(log_path).open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))[1:]
    for action, src, dst in reversed(rows):
        s, d = Path(dst), Path(src)
        if not s.exists():
            stats["missing"] = stats.get("missing", 0) + 1
            continue
        d.parent.mkdir(parents=True, exist_ok=True)
        final = _unique(d)
        try:
            shutil.move(str(s), str(final))
        except OSError:
            stats["failed"] = stats.get("failed", 0) + 1
            continue
        moves.append((str(s), str(final)))
        stats[action] = stats.get(action, 0) + 1
    if conn is not None:
        stats["catalog_repointed"] = _repoint(conn, moves)
    return stats


def default_plan_path(root: Path) -> Path:
    return Path(root).resolve() / ".mediaexplorer" / "vision-triage-plan.csv"


def default_log_path(root: Path) -> Path:
    return (Path(root).resolve() / ".mediaexplorer" /
            f"vision-triage-log-{time.strftime('%Y%m%d-%H%M%S')}.csv")
