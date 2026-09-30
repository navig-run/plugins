"""Execute a plan: copy, verify, then quarantine the source. The only mutating step.

**Why this is not ``navig.inbox.router.InboxRouter``.** The core router looks like the
right reuse and is not, for three reasons, the first of which is disqualifying:

* ``router.py`` calls ``_check_and_redirect`` unconditionally and *before* the
  confidence check, and that path can copy a file into a **sibling space**. It is inert
  today only because this space's ``routes.yaml`` happens to carry no ``exclude:``
  block — an accident of configuration, not a guarantee. Filing personal documents is
  exactly the case where that must be impossible by construction.
* Its destination filename is hardcoded to ``dest_dir / source_path.name``, so there
  is no seam for a canonical name — and renaming to the canonical form is half the
  point of this migration.
* ``RouteMode.MOVE`` is a bare ``shutil.move``: no hash verification, no quarantine,
  no undo.

So the executor is local, and mirrors the model proven in
``navig_explore.route`` — streaming copy that hashes the source in the same read pass,
verify by re-hashing the temp, ``os.replace`` into place, then move the source to a
quarantine that ``undo`` can replay. Nothing is ever hard-deleted.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from .plan import PlanRow
from .space import PaperworkPaths

CHUNK = 1 << 20


@dataclass
class ApplyStats:
    migrated: int = 0
    skipped: int = 0
    trashed_dupe: int = 0
    handoff: int = 0
    review: int = 0
    mismatch: int = 0
    collision: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.mismatch or self.collision or self.errors)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(CHUNK), b""):
                h.update(block)
    except OSError:
        return ""
    return h.hexdigest()


def _copy_verify(src: Path, tmp: Path) -> str:
    """Streaming copy src -> tmp, hashing the source in the same read pass.

    Returns the source digest if tmp is bit-identical, else "" (and removes tmp).
    One source read plus one temp read, with the same SHA-256 guarantee as copying
    and then hashing both.
    """
    h = hashlib.sha256()
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            for block in iter(lambda: fi.read(CHUNK), b""):
                h.update(block)
                fo.write(block)
        shutil.copystat(src, tmp)
    except OSError:
        tmp.unlink(missing_ok=True)
        return ""
    digest = h.hexdigest()
    if digest != _sha256(tmp):
        tmp.unlink(missing_ok=True)
        return ""
    return digest


def trash_path(src: Path, trash_root: Path) -> Path:
    """Quarantine location mirroring the source's own layout, per drive."""
    drive = (src.drive or "").replace(":", "").replace("\\", "") or "NODRIVE"
    parts = src.parts[1:] if src.drive else src.parts
    return trash_root.joinpath(drive, *parts)


def _to_trash(src: Path, trash_root: Path) -> Path:
    dest = trash_path(src, trash_root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest = dest.with_name(f"{dest.stem}-{_sha256(src)[:8]}{dest.suffix}")
    shutil.move(str(src), str(dest))
    return dest


def apply_plan(
    rows: Sequence[PlanRow],
    paths: PaperworkPaths,
    *,
    dry_run: bool = False,
    only: str | None = None,
    limit: int | None = None,
    on_progress: Callable[[int, PlanRow], None] | None = None,
) -> tuple[ApplyStats, Path | None]:
    """Execute *rows*. Returns (stats, receipt path). Idempotent and resumable."""
    st = ApplyStats()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    receipt = paths.receipt(stamp) if not dry_run else None
    if receipt is not None:
        receipt.parent.mkdir(parents=True, exist_ok=True)

    done = 0
    for n, row in enumerate(rows, 1):
        if limit and done >= limit:
            break
        if only and row.doc_class != only:
            continue

        if row.decision == "handoff":
            st.handoff += 1
            continue
        if row.decision in ("review", "skip"):
            st.review += 1
            continue

        src = Path(row.src)
        if on_progress:
            on_progress(n, row)

        if row.decision == "trash-dupe":
            if not src.exists():
                st.skipped += 1
                continue
            if dry_run:
                st.trashed_dupe += 1
                continue
            try:
                dest = _to_trash(src, paths.trash_root)
            except OSError as exc:
                st.errors.append(f"{src}: {exc}")
                continue
            st.trashed_dupe += 1
            _write_receipt(receipt, row, action="trashed-dupe", trash=str(dest))
            done += 1
            continue

        if row.decision != "migrate" or not row.dest_rel:
            st.review += 1
            continue

        dest = paths.space_root / row.dest_rel
        # The cardinal-rule guard: a destination outside this space is a bug, not a
        # thing to attempt. It can only happen through a hand-edited dest_rel.
        if not paths.contains(dest):
            st.errors.append(f"{row.row_id}: destination escapes the space: {row.dest_rel}")
            continue

        if dest.exists():
            existing = _sha256(dest)
            if existing == row.sha256:
                # Already migrated. Quarantine the source if it is still around; this
                # is what makes an interrupted run resumable and a rerun free.
                if src.exists() and not dry_run:
                    try:
                        moved = _to_trash(src, paths.trash_root)
                        _write_receipt(receipt, row, action="trashed-late", trash=str(moved))
                    except OSError as exc:
                        st.errors.append(f"{src}: {exc}")
                st.skipped += 1
                continue
            st.collision += 1
            st.errors.append(
                f"{row.row_id}: {row.dest_rel} exists with different content — left both in place"
            )
            continue

        if not src.exists():
            st.skipped += 1
            continue
        if dry_run:
            st.migrated += 1
            continue

        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".copytmp")
        digest = _copy_verify(src, tmp)
        if not digest:
            # SOURCE KEPT. A bad copy must never cost the original.
            st.mismatch += 1
            st.errors.append(f"{row.row_id}: copy verification failed for {src}")
            continue
        os.replace(tmp, dest)
        try:
            quarantined = str(_to_trash(src, paths.trash_root))
        except OSError as exc:
            # The document is filed; only the source cleanup failed. Report it, keep
            # the migration, and record an empty trash path so `undo` reports the
            # entry as unrecoverable rather than silently claiming success.
            st.errors.append(f"{src}: migrated but source not quarantined: {exc}")
            quarantined = ""
        st.migrated += 1
        _write_receipt(
            receipt, row, action="migrated", dest=str(dest),
            trash=quarantined, sha256=digest,
        )
        done += 1

    return st, receipt


def _write_receipt(
    receipt: Path | None,
    row: PlanRow,
    *,
    action: str,
    dest: str = "",
    trash: str = "",
    sha256: str = "",
) -> None:
    """Append one receipt line. Written *before* the next action, so an interrupted
    run is always undoable up to the last thing it actually did."""
    if receipt is None:
        return
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "row_id": row.row_id,
        "action": action,
        "src": row.src,
        "dest": dest,
        "trash": trash,
        "sha256": sha256 or row.sha256,
        "doc_class": row.doc_class,
    }
    with receipt.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
