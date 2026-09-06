"""Route a folder of audio into class folders, using :mod:`audio_class` predictions.

Plan first, move second — the same contract as :mod:`route`. The plan is a CSV you can
read before anything moves; ``apply`` then performs collision-safe moves and appends a
reversible log so ``undo`` can put every file back exactly where it came from.

Three rules this module exists to enforce:

- **Uncertainty gets its own folder, not a coin flip.** Anything under ``min_conf`` goes
  to ``_review/`` rather than being filed as a guess. A wrong confident move is far more
  expensive to undo by hand than a review pile is to skim.
- **Companions travel with their file.** A clip's ``.md`` transcript, ``.json`` sidecar
  or cover art shares its stem; moving the audio and orphaning the sidecar quietly
  destroys the pairing.
- **Never delete, never overwrite.** A name collision is resolved by suffixing, and every
  move is logged before the next one starts.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import time
from collections import Counter
from pathlib import Path

REVIEW = "_review"


def plan(records: list[dict], routes: dict[str, Path], *, min_conf: float = 0.6,
         review_root: Path | None = None, min_dur: float | None = None,
         max_dur: float | None = None, keep: set[str] | None = None) -> list[dict]:
    """Decide a destination for every classified record.

    ``records`` come from :func:`audio_class.classify_folder` (each has ``path``,
    ``label``, ``confidence``). A label with no route, or a confidence below
    ``min_conf``, lands in ``_review/<label>/`` under ``review_root``.

    ``min_dur`` / ``max_dur`` / ``keep`` narrow what the run is allowed to touch, which
    is what turns a whole-folder reorganisation into a **targeted cleanup**: "re-file the
    hour-long albums that ended up in my sound-effects library, and leave the actual
    sound effects exactly where they are". Anything outside the scope is marked
    ``action="leave"`` — reported, never moved, and never dumped into ``_review``.
    Silently filtering those files out instead would make the plan look like it covered
    the folder when it deliberately did not.
    """
    keep = keep or set()
    rows: list[dict] = []
    for r in records:
        src = Path(r["path"])
        label = r.get("label") or "_unknown"
        conf = float(r.get("confidence") or 0.0)
        dur = float(r.get("dur") or 0.0)

        out_of_scope = None
        if label in keep:
            out_of_scope = f"kept: label {label!r} left in place"
        elif min_dur is not None and dur < min_dur:
            out_of_scope = f"out of scope: {dur:.0f}s < min {min_dur:.0f}s"
        elif max_dur is not None and dur > max_dur:
            out_of_scope = f"out of scope: {dur:.0f}s > max {max_dur:.0f}s"
        if out_of_scope:
            rows.append({
                "src": str(src), "name": src.name, "label": label,
                "confidence": round(conf, 4), "dur": dur,
                "dest": str(src), "reason": out_of_scope,
                "review": False, "action": "leave",
            })
            continue

        dest_root = routes.get(label)
        if dest_root is not None and Path(dest_root) == src.parent:
            # Already where it belongs. Without this, `_unique` sees the destination
            # occupied — by this very file — and renames it to "name (2).ext", so
            # re-running a sort, or sorting a tree that contains its own destination
            # (Production/ contains Video Edit Music/), silently churns filenames.
            rows.append({
                "src": str(src), "name": src.name, "label": label,
                "confidence": round(conf, 4), "dur": dur,
                "dest": str(src), "reason": "already filed here",
                "review": False, "action": "leave",
            })
            continue

        reason = "ok"
        if dest_root is None:
            reason = "no route for label"
        elif conf < min_conf:
            reason = f"confidence {conf:.2f} < {min_conf:.2f}"
        if reason != "ok":
            base = review_root or (src.parent / REVIEW)
            dest_root = Path(base) / label
        rows.append({
            "src": str(src), "name": src.name, "label": label,
            "confidence": round(conf, 4), "dur": dur,
            "dest": str(Path(dest_root) / src.name), "reason": reason,
            "review": reason != "ok",
            "action": "review" if reason != "ok" else "move",
        })
    return rows


def write_plan(rows: list[dict], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["src", "name", "label", "confidence",
                                           "dur", "dest", "reason", "review", "action"])
        w.writeheader()
        w.writerows(rows)
    return path


def _action(r: dict) -> str:
    return r.get("action") or ("review" if r.get("review") else "move")


def summarize(rows: list[dict]) -> dict:
    filed = Counter(r["label"] for r in rows if _action(r) == "move")
    review = Counter(r["label"] for r in rows if _action(r) == "review")
    left = sum(1 for r in rows if _action(r) == "leave")
    out = {"total": len(rows), "filed": dict(filed), "review": dict(review),
           "review_total": sum(review.values())}
    if left:
        out["left_in_place"] = left
    return out


def _unique(dest: Path) -> Path:
    """A free path next to ``dest`` — never overwrite an existing file."""
    if not dest.exists():
        return dest
    stem, suf, parent = dest.stem, dest.suffix, dest.parent
    for i in range(2, 10000):
        cand = parent / f"{stem} ({i}){suf}"
        if not cand.exists():
            return cand
    return parent / f"{stem} ({int(time.time())}){suf}"


class _CompanionIndex:
    """stem → sidecar files, per directory, enumerated **once**.

    Correction to the note this replaced: an earlier version of this docstring blamed
    Windows directory enumeration for 28 sidecars left behind in a real run. That was
    wrong. Those 28 audio files lived in a ``_dupes`` subfolder while their ``.md``
    transcripts sat in the *parent*, so ``src.parent`` genuinely held no sidecar — the
    lookup was correct and the layout was the surprise. Do not go looking for a race
    here; there isn't one.

    Enumerating once is kept because it is simply better: O(1) per row instead of
    re-listing a directory for every file (a 17k-file folder made that quadratic), and
    handing each companion out exactly once keeps two same-stem files from claiming each
    other. It also sidesteps mutating a directory while iterating it, which
    :func:`os.walk` and friends explicitly do not promise to handle — worth avoiding on
    principle, even though it is not what bit us.
    """

    def __init__(self):
        self._dirs: dict[Path, dict[str, list[Path]]] = {}

    def _load(self, folder: Path) -> dict[str, list[Path]]:
        idx = self._dirs.get(folder)
        if idx is None:
            idx = {}
            try:
                for p in folder.iterdir():
                    if p.is_file():
                        idx.setdefault(p.stem, []).append(p)
            except OSError:
                idx = {}
            self._dirs[folder] = idx
        return idx

    def take(self, src: Path) -> list[Path]:
        """Sidecars sharing this file's stem (``.md`` transcript, ``.json``, cover art).

        Each companion is handed out once — a stem shared by two audio files must not
        make them each other's "sidecar" and drag one along unclassified.
        """
        idx = self._load(src.parent)
        mates = idx.get(src.stem, [])
        out = [p for p in mates if p != src and p.suffix.lower() not in AUDIO_SUFFIXES]
        idx[src.stem] = [p for p in mates if p not in out]
        return out


# a same-stem *audio* file is a separate work item, not a sidecar to be dragged along
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus",
                  ".wma", ".aif", ".aiff", ".octet-stream"}


def apply(rows: list[dict], *, log_path: Path, move_companions: bool = True,
          quiet: bool = False) -> dict:
    """Execute the plan. Returns counts; every move is appended to ``log_path`` first."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stats = Counter()
    companions = _CompanionIndex()
    with log_path.open("a", encoding="utf-8") as log:
        for r in rows:
            if _action(r) == "leave":
                stats["left"] += 1
                continue
            src = Path(r["src"])
            if not src.exists():
                stats["missing"] += 1
                continue
            dest_dir = Path(r["dest"]).parent
            dest_dir.mkdir(parents=True, exist_ok=True)
            pairs = [(src, _unique(Path(r["dest"])))]
            if move_companions:
                for c in companions.take(src):
                    pairs.append((c, _unique(dest_dir / c.name)))
            for s, d in pairs:
                try:
                    # log before the move: a crash mid-run must leave a recoverable trail
                    log.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                          "src": str(s), "dst": str(d),
                                          "label": r["label"],
                                          "confidence": r["confidence"]},
                                         ensure_ascii=False) + "\n")
                    log.flush()
                    shutil.move(str(s), str(d))
                    stats["moved" if s == src else "companions"] += 1
                except OSError as e:
                    stats["failed"] += 1
                    if not quiet:
                        print(f"  ! {s.name}: {e}", flush=True)
    return dict(stats)


def undo(log_path: Path, *, quiet: bool = False) -> dict:
    """Move every logged file back to where it came from (newest entries first)."""
    entries = []
    for line in log_path.open(encoding="utf-8", errors="replace"):
        try:
            entries.append(json.loads(line))
        except Exception:  # noqa: BLE001
            pass
    stats = Counter()
    for e in reversed(entries):
        dst, src = Path(e["dst"]), Path(e["src"])
        if not dst.exists():
            stats["missing"] += 1
            continue
        src.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(dst), str(_unique(src)))
            stats["restored"] += 1
        except OSError as exc:
            stats["failed"] += 1
            if not quiet:
                print(f"  ! {dst.name}: {exc}", flush=True)
    return dict(stats)


def prune_empty(root: Path) -> int:
    """Remove directories left empty by the moves (never touches files)."""
    removed = 0
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        if dirpath == str(root):
            continue
        # Re-read the directory instead of trusting os.walk's captured lists: a parent
        # becomes empty *during* the bottom-up walk, after its entry was snapshotted.
        try:
            if os.listdir(dirpath):
                continue
            os.rmdir(dirpath)
            removed += 1
        except OSError:
            pass
    return removed
