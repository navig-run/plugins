"""Mine the library: collect videos matching filters into a working edit folder.

Hardlinks matches (instant, zero extra space on the same volume) with a copy
fallback, and writes a ``_selection.csv`` manifest. This is the 'pull a themed
selection for an edit' primitive — e.g. all 4K clips from 2024, or camera+drone
footage — dropped into a folder any editor can open, deletable without touching
the originals.

Reads the ``.mediaexplorer/meta.jsonl`` index (from ``probe``).
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import sys
from pathlib import Path


def collect(root: Path, dest: Path, *, res: str | None = None,
            source: str | None = None, year: int | None = None,
            typ: str | None = "video", quiet: bool = False) -> dict:
    def log(*a):
        if not quiet:
            print(*a, flush=True)

    root, dest = Path(root).resolve(), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    meta = root / ".mediaexplorer" / "meta.jsonl"
    if not meta.exists():
        log(f"[collect] no index at {meta} — run `navig explore probe {root}` first")
        return {}
    linked = copied = miss = 0
    rows = []
    for line in meta.open(encoding="utf-8", errors="replace"):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if typ and r.get("type") != typ:
            continue
        if res and r.get("res_tier") != res:
            continue
        if source and r.get("source_class") != source:
            continue
        if year and r.get("year") != year:
            continue
        src = r.get("abs") or str(root / r["rel"])
        if not os.path.exists(src):
            miss += 1
            continue
        parent = Path(r["rel"]).parent.name
        base = dest / (f"{parent}__{Path(src).name}" if parent else Path(src).name)
        out, i = base, 1
        while out.exists():
            out = base.with_name(f"{base.stem}_{i}{base.suffix}")
            i += 1
        try:
            os.link(src, out)          # hardlink: 0 extra bytes, same volume
            linked += 1
        except Exception:  # noqa: BLE001 — cross-volume / FS without hardlinks
            try:
                shutil.copy2(src, out)
                copied += 1
            except Exception:  # noqa: BLE001
                miss += 1
                continue
        rows.append([r.get("res_tier"), r.get("source_class"), r.get("year"),
                     r.get("dur"), r.get("size"), src, str(out)])
    with (dest / "_selection.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["res_tier", "source_class", "year", "dur", "size", "src", "link"])
        w.writerows(rows)
    gb = round(sum((r[4] or 0) for r in rows) / 1024**3, 1)
    summary = {"clips": len(rows), "gb": gb, "hardlinked": linked,
               "copied": copied, "missing": miss, "dest": str(dest)}
    log(f"[collect] {json.dumps(summary, ensure_ascii=False)}")
    return summary


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Collect a themed selection into an edit folder")
    ap.add_argument("root")
    ap.add_argument("dest")
    ap.add_argument("--res", default=None, help="res tier: 4K|QHD|FHD|HD|SD")
    ap.add_argument("--source", default=None, help="source class: phone|camera|drone|screen|old")
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--type", default="video")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    collect(Path(a.root), Path(a.dest), res=a.res, source=a.source, year=a.year,
            typ=a.type, quiet=a.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
