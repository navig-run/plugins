"""Where a photo was taken — measured, propagated, or read off a folder name.

Ordered by how much each source actually yields on a salvage library, which is
not the order you would guess:

1. ``exif``       -- real GPS in the file. Rare here (5 of 4,838) but exact.
2. ``propagated`` -- a GPS-tagged neighbour in the same folder within a few
                     hours. This is the big one: cameras and phones drop GPS
                     intermittently, so a handful of tagged frames can place a
                     whole afternoon.
3. ``folder``     -- a place name parsed out of a human-named event folder
                     ("Берлин 2014", "2004 - Greece") matched against GeoNames.
                     Free, precise when it hits, and something no off-the-shelf
                     photo manager looks at.

Reverse geocoding is fully offline: GeoNames ``cities15000`` plus a
``scipy.spatial.cKDTree``, about thirty lines. ``reverse-geocoder`` on PyPI does
the same thing but is LGPL and unmaintained since 2023, and PhotoPrism's Places
enrichment sends your coordinates to their servers. Neither trade is worth it.

``geo_source`` is stored on every row, so a folder-name guess can never be
mistaken for a measured fix.
"""
from __future__ import annotations

import csv
import io
import os
import zipfile
from pathlib import Path

GEONAMES_URL = "https://download.geonames.org/export/dump/cities15000.zip"
CONFIDENCE = {"exif": 1.0, "propagated": 0.75, "folder": 0.55}

# How far apart two photos may be in time and still share a location.
PROPAGATE_WINDOW_HOURS = 6

_gazetteer: list[tuple[str, str, str, float, float]] | None = None
_tree = None


def cache_dir() -> Path:
    d = Path(os.environ.get("NAVIG_HOME", Path.home() / ".navig")) / "cache" / "geonames"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_gazetteer(*, quiet: bool = False) -> Path:
    """Download GeoNames cities15000 once (~2 MB)."""
    dest = cache_dir() / "cities15000.txt"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    import urllib.request  # noqa: PLC0415

    if not quiet:
        print("[places] fetching GeoNames cities15000 …", flush=True)
    with urllib.request.urlopen(GEONAMES_URL, timeout=120) as resp:  # noqa: S310
        blob = resp.read()
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        dest.write_bytes(z.read("cities15000.txt"))
    return dest


def load_gazetteer(*, quiet: bool = False):
    """→ ``(rows, cKDTree)`` where a row is (name, admin1, country, lat, lon)."""
    global _gazetteer, _tree  # noqa: PLW0603 - process-level cache, built once
    if _gazetteer is not None:
        return _gazetteer, _tree

    import numpy as np  # noqa: PLC0415
    from scipy.spatial import cKDTree  # noqa: PLC0415

    path = ensure_gazetteer(quiet=quiet)
    rows: list[tuple[str, str, str, float, float, int]] = []
    with path.open(encoding="utf-8", newline="") as fh:
        for rec in csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE):
            if len(rec) < 15:
                continue
            try:
                lat, lon = float(rec[4]), float(rec[5])
            except ValueError:
                continue
            try:
                pop = int(rec[14])
            except (ValueError, IndexError):
                pop = 0
            rows.append((rec[1], rec[10], rec[8], lat, lon, pop))

    # Compare on the unit sphere, not on raw degrees: a degree of longitude is
    # 111 km at the equator and 30 km in Scandinavia, so a flat kd-tree over
    # lat/lon picks visibly wrong cities at high latitudes.
    lat = np.radians([r[3] for r in rows])
    lon = np.radians([r[4] for r in rows])
    xyz = np.column_stack([np.cos(lat) * np.cos(lon),
                           np.cos(lat) * np.sin(lon),
                           np.sin(lat)])
    _gazetteer, _tree = rows, cKDTree(xyz)
    return _gazetteer, _tree


def reverse(lat: float, lon: float) -> tuple[str, str, str] | None:
    """Nearest populated place → ``(name, admin1, country)``."""
    import numpy as np  # noqa: PLC0415

    rows, tree = load_gazetteer(quiet=True)
    if not rows:
        return None
    la, lo = np.radians(lat), np.radians(lon)
    pt = [np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)]
    _dist, idx = tree.query(pt)
    name, admin1, country = rows[int(idx)][:3]
    return name, admin1, country


# A folder name is weak evidence, so ignore hamlets — but keep this low, because
# the country prior below is what actually does the disambiguating. Set to 50,000
# it discarded Frontignan and Millau, both genuinely this library's home region.
FOLDER_MIN_POPULATION = 15_000


def _candidates(rows, *, min_population: int = 0) -> dict[str, list[int]]:
    """name -> every place with that name, most populous first."""
    out: dict[str, list[int]] = {}
    for i, row in enumerate(rows):
        if row[5] < min_population:
            continue
        key = row[0].strip().lower()
        if key:
            out.setdefault(key, []).append(i)
    for key, idxs in out.items():
        idxs.sort(key=lambda i: rows[i][5], reverse=True)
    return out


def home_countries(conn) -> set[str]:
    """Countries this library demonstrably contains, from its measured GPS.

    A folder called `london` could be in the UK or Ontario, and `martin` is a
    Slovak city as readily as a French saint. Population alone gets London right
    and Frontignan wrong. But the library's own EXIF fixes say where its owner
    actually took photographs, so prefer those countries and fall back to
    population only when none matches. Self-tuning, and it needs no configuration.
    """
    return {r["country"] for r in conn.execute(
        "SELECT DISTINCT country FROM geo WHERE source='exif' AND country IS NOT NULL")
        if r["country"]}


# A place nobody in this library has ever demonstrably been is only believable if
# it is famous enough that the folder can't plausibly mean anything else.
# Amsterdam, Minsk and London clear this; Martin (Slovakia), Belmont, Toma and
# Mexico-the-Philippine-town do not — and those were all coincidences.
FOLDER_UNAMBIGUOUS_POPULATION = 500_000


def _pick(rows, idxs: list[int], home: set[str]) -> int | None:
    """Choose a candidate, or reject the match entirely.

    Reordering alone is not enough. If no candidate sits in a country this
    library actually contains, the word is far more likely to be a person's name
    or an ordinary noun than a place — so return None rather than settling for
    the most populous stranger.
    """
    for i in idxs:
        if rows[i][2] in home:
            return i
    return idxs[0] if rows[idxs[0]][5] >= FOLDER_UNAMBIGUOUS_POPULATION else None


# Words that are library taxonomy or generic description, never a location —
# even though GeoNames has a populated place by each of these names.
_NEVER_A_PLACE = {
    "date", "dates", "album", "albums", "photo", "photos", "picture", "pictures",
    "image", "images", "video", "videos", "camera", "cameras", "webcam", "archive",
    "archives", "general", "people", "person", "family", "friends", "theme",
    "themes", "filter", "country", "countries", "city", "cities", "misc", "other",
    "new", "old", "best", "sorted", "unsorted", "recovered", "backup", "temp",
    "orange", "green", "blue", "red", "black", "white", "products", "thumbnails",
    "media", "files", "phone", "mobile", "desktop", "screenshot", "screenshots",
}


def structural_components(conn, root: Path, *, share: float = 0.02,
                          minimum: int = 500) -> set[str]:
    """Path components so common they are the library's shelving, not a place.

    Derived from the data rather than hard-coded, the same way the junk-mtime
    detector works — a folder that 70% of the library sits under is a taxonomy
    node whatever it happens to be called.

    Without this, `By Date` — the top-level folder of most of this library —
    matched a Japanese city called *Date* and mislabelled **68,351 photos**.
    """
    from collections import Counter  # noqa: PLC0415

    rows = conn.execute(
        "SELECT rel FROM files WHERE present=1 AND root=?",
        (str(Path(root).resolve()),)).fetchall()
    if not rows:
        return set()
    counts: Counter = Counter()
    for r in rows:
        for part in Path(r["rel"]).parts[:-1]:
            counts[part.strip().lower()] += 1
    threshold = max(minimum, int(len(rows) * share))
    return {p for p, n in counts.items() if n >= threshold}


def resolve(root: Path, *, do_folder: bool = True, quiet: bool = False) -> dict:
    """Fill the ``geo`` table from EXIF, then neighbours, then folder names."""
    from datetime import datetime  # noqa: PLC0415

    from . import catalog  # noqa: PLC0415

    root = Path(root).resolve()
    conn = catalog.connect(root)
    stats = {"exif": 0, "propagated": 0, "folder": 0}

    def store(rows):
        if rows:
            conn.executemany(
                """INSERT INTO geo (sha256, lat, lon, place, admin1, country,
                                    source, confidence)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(sha256) DO UPDATE SET
                     lat=excluded.lat, lon=excluded.lon, place=excluded.place,
                     admin1=excluded.admin1, country=excluded.country,
                     source=excluded.source, confidence=excluded.confidence
                   WHERE excluded.confidence > geo.confidence""", rows)
            conn.commit()

    files = conn.execute(
        """SELECT sha256, rel, p_gps_lat AS lat, p_gps_lon AS lon
           FROM files WHERE present=1 AND root=? AND sha256 IS NOT NULL""",
        (str(root),)).fetchall()

    # 1 — measured GPS
    batch = []
    for r in files:
        if r["lat"] is None or r["lon"] is None:
            continue
        got = reverse(r["lat"], r["lon"])
        batch.append((r["sha256"], r["lat"], r["lon"],
                      got[0] if got else None, got[1] if got else None,
                      got[2] if got else None, "exif", CONFIDENCE["exif"]))
    store(batch)
    stats["exif"] = len(batch)

    # 2 — propagate to time-near neighbours in the same folder
    dated = {r["sha256"]: r["value"] for r in conn.execute(
        "SELECT sha256, value FROM dates")}
    anchors: dict[str, list[tuple]] = {}
    for r in files:
        if r["lat"] is None:
            continue
        anchors.setdefault(str(Path(r["rel"]).parent), []).append(
            (dated.get(r["sha256"]), r["lat"], r["lon"]))
    batch = []
    have_geo = {r["sha256"] for r in conn.execute("SELECT sha256 FROM geo")}
    for r in files:
        if r["sha256"] in have_geo:
            continue
        near = anchors.get(str(Path(r["rel"]).parent))
        if not near:
            continue
        when = dated.get(r["sha256"])
        chosen = None
        for a_when, a_lat, a_lon in near:
            if not when or not a_when:
                continue
            try:
                gap = abs((datetime.fromisoformat(when)
                           - datetime.fromisoformat(a_when)).total_seconds())
            except ValueError:
                continue
            if gap <= PROPAGATE_WINDOW_HOURS * 3600:
                chosen = (a_lat, a_lon)
                break
        if chosen is None:
            continue
        got = reverse(*chosen)
        batch.append((r["sha256"], chosen[0], chosen[1],
                      got[0] if got else None, got[1] if got else None,
                      got[2] if got else None, "propagated", CONFIDENCE["propagated"]))
    store(batch)
    stats["propagated"] = len(batch)

    # 3 — place names in human-named event folders
    if do_folder:
        rows, _ = load_gazetteer(quiet=quiet)
        names = _candidates(rows, min_population=FOLDER_MIN_POPULATION)
        skip = structural_components(conn, root) | _NEVER_A_PLACE
        home = home_countries(conn)
        stats["folder_skipped_components"] = len(skip)
        stats["home_countries"] = len(home)
        have_geo = {r["sha256"] for r in conn.execute("SELECT sha256 FROM geo")}
        batch = []
        for r in files:
            if r["sha256"] in have_geo:
                continue
            hit = None
            for part in reversed(Path(r["rel"]).parts[:-1]):
                if part.strip().lower() in skip:
                    continue
                for tok in _tokens(part):
                    if tok in skip or tok in _NEVER_A_PLACE:
                        continue
                    if tok in names:
                        pick = _pick(rows, names[tok], home)
                        if pick is not None:
                            hit = rows[pick]
                            break
                if hit:
                    break
            if not hit:
                continue
            name, admin1, country, lat, lon = hit[:5]
            batch.append((r["sha256"], lat, lon, name, admin1, country,
                          "folder", CONFIDENCE["folder"]))
        store(batch)
        stats["folder"] = len(batch)

    if not quiet:
        print(f"[places] {stats}", flush=True)
    return stats


def _tokens(part: str) -> list[str]:
    """Candidate place names from a folder component, longest first.

    Longest-first because "New York" must beat "New", and a two-word city is
    exactly the case a naive word-split gets wrong.
    """
    import re  # noqa: PLC0415

    words = [w for w in re.split(r"[^\wÀ-ÿА-яЁё]+", part) if len(w) > 2
             and not w.isdigit()]
    out = []
    for n in (3, 2, 1):
        for i in range(len(words) - n + 1):
            out.append(" ".join(words[i:i + n]).lower())
    return out
