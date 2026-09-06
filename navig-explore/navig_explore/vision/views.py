"""The standard library view tree — one command, everything in its place.

Building views facet-by-facet produced a `by-year` holding 51,459 entries of which
only 27,767 were photographs; the rest were webcam frames, screenshots, web
graphics and documents dumped into the same year folders. The photos were all
there — 14,940 of 14,968 iPhone files among them — and completely unfindable.

So the views are defined here as a set rather than assembled by hand:

* every photo-facing view (**year, event, person, place**) is restricted to
  ``class = photo``;
* everything else gets its own top-level bucket, so nothing is hidden and nothing
  contaminates the photographs;
* the tree lives in a folder every scanner in this repo walks past, which is why
  it can sit inside the library instead of on a sibling drive.

All of it is hardlinks: one file on disk with several names. The tree costs no
space and deleting it cannot lose a photo.
"""
from __future__ import annotations

from pathlib import Path

from . import arrange

#: Folder under the library root. In every scanner's skip list, so a tree here is
#: invisible to probe/index/dedupe/gallery while sitting beside the photos.
DEFAULT_SUBDIR = "_organized"

PHOTO = ("photo",)

#: (folder, facet, kwargs) — order is the order they are reported in.
#: Photographs get the browsable facets. Everything else is a pile you occasionally
#: need to find something in — one folder, not one folder per year, because
#: "webcam frames from 2011" is not a question anyone asks.
VIEWS: tuple[tuple[str, str, dict], ...] = (
    ("by-year", "year", {"only_classes": PHOTO}),
    ("by-subject", "subject", {"only_classes": PHOTO}),
    ("by-event", "event", {"only_classes": PHOTO}),
    ("by-person", "person", {"only_classes": PHOTO, "include_unnamed": True}),
    ("by-place", "place", {"only_classes": PHOTO}),
    ("screenshots", "screenshot-type", {"only_classes": ("screenshot",)}),
    ("documents", "document-type", {"only_classes": ("document",)}),
    ("webcam", arrange.FLAT, {"only_classes": ("webcam",)}),
    ("web-graphics", arrange.FLAT, {"only_classes": ("web-graphic",)}),
    ("unsorted", arrange.FLAT, {"only_classes": ("unsorted",)}),
)

#: View folders that concentrate documents nobody should hand to a sync client.
#:
#: Sorting by kind is what created the hazard: passports were scattered through a
#: 169,000-file library and are now 78 files in one folder, and the finance
#: screenshots — bank balances, transaction lists, card numbers — are 401 more.
#: That is a real improvement for finding them and a real risk if the tree is
#: ever synced, so the tree says so about itself.
SENSITIVE: dict[str, str] = {
    "documents/identity": "passports, ID cards and driving licences",
    "documents/receipt": "invoices and bank statements",
    "screenshots/finance": "bank balances, transaction lists and payment confirmations",
    "screenshots/chat": "private conversations",
    "screenshots/email": "personal correspondence",
}

README_NAME = "_READ-ME-FIRST.md"


def write_manifest(root: Path, dest: Path, summary: dict, events: dict | None = None) -> Path:
    """Describe the tree inside the tree.

    Two facts about this view tree are invisible from within it and were each
    found the hard way: which folders concentrate sensitive documents, and which
    occasions exist in the catalog but can have no folder because every file in
    them is unreadable. A note beside the folders is where someone will actually
    encounter them.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    lines = [
        "# What is in here", "",
        "Every file below is a **hardlink** — the same photograph as the one in the",
        "library, under another name. Deleting anything here cannot lose a photo, and",
        "the whole tree costs no disk space. It is rebuilt by",
        "`navig explore photos views <root> --apply`.", "",
        "## Views", "",
    ]
    for folder, _facet, _kw in VIEWS:
        s = summary.get(folder)
        if s:
            lines.append(f"- `{folder}/` — {s['links']:,} files in {s['folders']:,} folders")
    lines += ["", "## ⚠ Do not sync or share this tree", "",
              "Filing by kind concentrated what used to be scattered:", ""]
    for path, what in SENSITIVE.items():
        lines.append(f"- `{path}/` — {what}")
    lines += ["",
              "Point no backup, cloud sync or gallery share at `_organized`. The",
              "originals are already backed up; this tree is regenerable in minutes.", ""]
    if events and events.get("unviewable_events"):
        lines += [
            "## Occasions with nothing left to show", "",
            f"{events['unviewable_events']} folders you named by hand have **no readable",
            "file left** — every image inside is unrecoverable bytes from a bad recovery",
            "run. They have no folder here, because a view is for looking at things; the",
            "names survive in the catalog and in `_lost-occasions.txt` beside this file.",
            "",
        ]
    out = dest / README_NAME
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def lost_occasions(conn) -> list:
    """Named occasions where **every** file is unreadable — the ones with no folder.

    The filter has to be per EVENT, not per file. Filtering rows to the
    undecodable ones and then grouping lists any event with a *single* damaged
    file, which reported 51 lost occasions against the 20 `photos events` counts —
    two numbers describing the same thing and disagreeing. A partially damaged
    occasion still has a folder and is not lost.
    """
    return conn.execute("""
        SELECT e.year, e.event_name, COUNT(*) AS n
        FROM events e
        WHERE e.source = 'folder'
        GROUP BY e.year, e.event_key
        HAVING SUM(CASE WHEN EXISTS (SELECT 1 FROM assets a
                        WHERE a.sha256 = e.sha256 AND a.decoded = 1)
                   THEN 1 ELSE 0 END) = 0
        ORDER BY e.year, e.event_name""").fetchall()


def write_lost_occasions(conn, root: Path, dest: Path) -> Path | None:
    """List the named occasions whose every file is unreadable, so the names survive."""
    rows = lost_occasions(conn)
    if not rows:
        return None
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / "_lost-occasions.txt"
    body = ["# Occasions with no readable file left",
            "#",
            "# You named these folders. Every image inside is unreadable bytes — the",
            "# damage is from a data-recovery run, not from this tooling, and nothing",
            "# here deleted anything. The name is the only surviving record.",
            "#", "# year  files  name", ""]
    body += [f"{r['year']}  {r['n']:5}  {r['event_name']}" for r in rows]
    out.write_text("\n".join(body) + "\n", encoding="utf-8")
    return out


def coverage(conn, root: Path, min_confidence: float) -> dict:
    """What the year views can and cannot show, so the gap is stated not hidden."""
    root = str(Path(root).resolve())
    q = lambda sql, a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    dated = q("""SELECT COUNT(*) FROM files f JOIN dates d ON d.sha256=f.sha256
                 WHERE f.present=1 AND f.root=?""", (root,))
    strong = q("""SELECT COUNT(*) FROM files f JOIN dates d ON d.sha256=f.sha256
                  WHERE f.present=1 AND f.root=? AND d.confidence>=?""",
               (root, min_confidence))
    return {"dated": dated, "confident": strong}


def build_plan(root: Path, dest: Path, *, min_group: int = 30,
               min_distinct_photos: int = arrange.MIN_DISTINCT_PHOTOS,
               min_confidence: float = arrange.MIN_DATE_CONFIDENCE
               ) -> tuple[list[dict], dict]:
    """Plan every view at once. Pure — touches nothing on disk."""
    root = Path(root).resolve()
    dest = Path(dest).expanduser().resolve()
    rows: list[dict] = []
    summary: dict[str, dict] = {}

    for folder, facet, kw in VIEWS:
        opts = dict(kw)
        if facet == "person":
            opts.update(min_group=min_group, min_distinct_photos=min_distinct_photos)
        sub, buckets = arrange.build_plan(
            root, dest / folder, facet, min_confidence=min_confidence, **opts)
        for r in sub:
            rows.append({**r, "view": folder})
        summary[folder] = {"links": len(sub), "folders": len(buckets)}
    return rows, summary


def write_plan(rows: list[dict], out: Path) -> Path:
    import csv  # noqa: PLC0415

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["view", "bucket", "src", "dst"])
        w.writeheader()
        w.writerows(rows)
    return out


def default_dest(root: Path) -> Path:
    return Path(root).resolve() / DEFAULT_SUBDIR


def default_log(root: Path) -> Path:
    import time  # noqa: PLC0415

    return (Path(root).resolve() / ".mediaexplorer" /
            f"views-log-{time.strftime('%Y%m%d-%H%M%S')}.csv")
