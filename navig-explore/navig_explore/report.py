"""Phase-0 recon report for the Y: -> X: consolidation.

Reads the ``meta.jsonl`` (probe) and ``dupes.jsonl`` (dedup) sidecars and writes a
human-readable ``PHASE0-REPORT.md`` plus supporting CSVs, so the operator can
review the full inventory + the exact routing + the dedup reclaim BEFORE any file
moves (the Phase-0 review gate).

    python report.py <root> [<root2> ...] --out <dir>
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from navig_explore.route import route_dest  # reuse the single source of routing truth


def _load_meta(root: Path) -> list[dict]:
    mp = root / ".mediaexplorer" / "meta.jsonl"
    out, seen = [], set()
    if not mp.exists():
        return out
    for line in mp.open(encoding="utf-8", errors="replace"):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if r.get("rel") and r["rel"] not in seen:
            seen.add(r["rel"])
            out.append(r)
    return out


def _load_dupes(root: Path) -> list[dict]:
    dp = root / ".mediaexplorer" / "dupes.jsonl"
    out = []
    if dp.exists():
        for line in dp.open(encoding="utf-8", errors="replace"):
            try:
                out.append(json.loads(line))
            except Exception:  # noqa: BLE001
                pass
    return out


def _gb(b: int) -> float:
    return round(b / 1024**3, 1)


def _bucket(dest_rel: str) -> str:
    """Collapse a destination to a reporting bucket (2-3 segments)."""
    parts = dest_rel.split("/")
    if parts[0] == "Photos":
        return "Photos"
    if parts[0] == "Audio":
        return f"Audio/{parts[1]}" if len(parts) > 2 else "Audio"
    if parts[0] == "Video":
        if len(parts) >= 3 and parts[1] == "somaleto":
            return f"Video/somaleto/{parts[2]}"
        return f"Video/{parts[1]}" if len(parts) > 1 else "Video"
    return parts[0]


def build_report(roots: list[Path], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    recs: list[dict] = []
    autotrash: set[str] = set()
    dupe_stats = {"exact_groups": 0, "exact_files": 0, "exact_gb": 0.0,
                  "near_image": 0, "near_video": 0}
    for root in roots:
        rs = _load_meta(root)
        for r in rs:
            r["_root"] = str(root)
        recs.extend(rs)
        for g in _load_dupes(root):
            if g.get("kind") == "exact":
                dupe_stats["exact_groups"] += 1
                at = g.get("auto_trash", [])
                dupe_stats["exact_files"] += len(at)
                dupe_stats["exact_gb"] += _gb(g.get("size", 0) * len(at))
                autotrash.update(at)
            elif g.get("kind") == "near-image":
                dupe_stats["near_image"] += 1
            elif g.get("kind") == "near-video":
                dupe_stats["near_video"] += 1

    total = len(recs)
    total_gb = _gb(sum(r.get("size") or 0 for r in recs))
    by_type = Counter(r.get("type") for r in recs)
    gb_type = defaultdict(float)
    for r in recs:
        gb_type[r.get("type")] += (r.get("size") or 0)
    by_class = Counter(r.get("source_class") for r in recs if r.get("type") == "video")
    by_tier = Counter(r.get("res_tier") for r in recs if r.get("type") == "video")
    by_year = Counter(r.get("year") for r in recs)
    by_codec = Counter(r.get("codec") for r in recs if r.get("type") == "video")
    fails = sum(1 for r in recs if not r.get("probe_ok"))

    # routing breakdown (skip exact-dupes → they go to .trash, not a bucket)
    buckets_n: Counter = Counter()
    buckets_gb: defaultdict = defaultdict(float)
    manifest_rows = []
    for r in recs:
        rel = r["rel"]
        if rel in autotrash:
            dest = f".trash/{rel}"
            b = ".trash (exact dupe)"
        else:
            dest = route_dest(r)
            b = _bucket(dest)
        buckets_n[b] += 1
        buckets_gb[b] += (r.get("size") or 0)
        manifest_rows.append([b, r.get("type"), r.get("source_class"),
                              r.get("res_tier"), r.get("year"), r.get("size"),
                              r.get("abs") or rel, dest])

    # write full manifest CSV
    mani = out_dir / "routing-manifest.csv"
    with mani.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["bucket", "type", "source_class", "res_tier", "year", "size",
                    "src", "dest_rel"])
        w.writerows(manifest_rows)

    def tbl(counter, gbmap=None, top=None):
        items = counter.most_common(top) if top else sorted(
            counter.items(), key=lambda kv: -(gbmap[kv[0]] if gbmap else kv[1]))
        lines = []
        for k, n in items:
            g = f" · {_gb(gbmap[k])} GB" if gbmap else ""
            lines.append(f"- `{k}` — {n:,} files{g}")
        return "\n".join(lines)

    md = out_dir / "PHASE0-REPORT.md"
    with md.open("w", encoding="utf-8") as f:
        f.write(f"# Phase 0 — Recon Report\n\n")
        f.write(f"Roots: {', '.join(str(r) for r in roots)}\n\n")
        f.write(f"**{total:,} media files · {total_gb:,} GB** "
                f"(probe failures: {fails})\n\n")
        f.write("## By type\n" + tbl(by_type, gb_type) + "\n\n")
        f.write("## Video by source-class (heuristic)\n" + tbl(by_class) + "\n\n")
        f.write("## Video by resolution tier\n" + tbl(by_tier) + "\n\n")
        f.write("## Video by codec\n" + tbl(by_codec, top=12) + "\n\n")
        f.write("## By year (capture/mtime)\n" + tbl(by_year, top=20) + "\n\n")
        f.write("## Dedup\n")
        f.write(f"- Exact duplicate groups: **{dupe_stats['exact_groups']:,}** → "
                f"**{dupe_stats['exact_files']:,} files** auto-to-`.trash`, "
                f"**~{round(dupe_stats['exact_gb'],1):,} GB** reclaimable\n")
        f.write(f"- Near-dupe IMAGE clusters (flag-only, review): "
                f"{dupe_stats['near_image']:,}\n")
        f.write(f"- Near-dupe VIDEO clusters (flag-only, review): "
                f"{dupe_stats['near_video']:,}\n\n")
        f.write("## Routing — where every file goes on X:\\ (post-dedup)\n")
        f.write("| Destination bucket | Files | GB |\n|---|---:|---:|\n")
        for b in sorted(buckets_n, key=lambda k: -buckets_gb[k]):
            f.write(f"| `{b}` | {buckets_n[b]:,} | {_gb(buckets_gb[b]):,} |\n")
        f.write(f"\nFull per-file plan: `{mani.name}`\n")
    return md


def main(argv=None):
    ap = argparse.ArgumentParser(description="Phase-0 recon report")
    ap.add_argument("roots", nargs="+")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    md = build_report([Path(r).resolve() for r in a.roots], Path(a.out))
    print(f"[report] wrote {md}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
