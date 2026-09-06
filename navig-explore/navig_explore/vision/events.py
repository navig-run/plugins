"""Which occasion a photograph belongs to.

This library is curated in half and bare in the other half. Named event folders
crowd the hand-sorted years — 27 to 81 per year across 2003-2010 — and then
collapse to almost nothing from 2011 on, where everything sits in bare `YYYY-MM`
buckets. Measured: 11,948 photographs have an event, 29,166 do not.

So there are two sources, and they are not equal:

* **`folder`** — a photo under ``By Date/<year>/<name>/`` where the name is not a
  date bucket. That name was chosen by a human and no clustering may overrule it.
* **`detected`** — everything else with a date solid enough to act on, split
  wherever the gap between consecutive photographs reaches ``gap_hours``.

**The threshold is measured, not guessed.** Inter-photo gaps across this library
run p85 0.99 h · p90 6.53 h · p95 28.6 h. A 24-hour split yields 1,470 events
averaging 20.3 photographs — and the operator's own 336 named events have a
median of 20. The default reproduces their existing sense of an occasion.

Names are date-prefixed so one year folder sorts chronologically. That matches
what the operator already does by hand (``2008.08.10_teuf marseille``), and a
curated name is only ever prefixed, never rewritten.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from pathlib import Path

#: Gap between consecutive photographs that starts a new occasion.
DEFAULT_GAP_HOURS = 24.0

#: Fewer than this and it is a stray, not an event worth a folder.
DEFAULT_MIN_SIZE = 3

#: Only a date this solid may place a photo in time.
MIN_DATE_CONFIDENCE = 0.80

#: `By Date/2009/2009-02` is a bucket; `By Date/2009/Alzon 2009` is an occasion.
_DATE_BUCKET = re.compile(r"^\d{4}-\d{2}$")

_SEP = " · "

#: Dates the operator writes into their own folder names, most specific first.
#: `2008.08.10_teuf marseille` · `06.04.07 - chez adrien` · `02.07.2005 - fete`.
#: Two-digit years are theirs too, and every one of them is 2000s: this library
#: starts in 1999 and the earliest such folder is 2003.
_NAME_DATES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<!\d)(?P<y>(?:19|20)\d{2})[.\-_/](?P<m>\d{1,2})[.\-_/](?P<d>\d{1,2})(?!\d)"),
     "ymd"),
    (re.compile(r"(?<!\d)(?P<d>\d{1,2})[.\-_/](?P<m>\d{1,2})[.\-_/](?P<y>(?:19|20)\d{2})(?!\d)"),
     "dmy"),
    (re.compile(r"(?<!\d)(?P<d>\d{1,2})[.\-_/](?P<m>\d{1,2})[.\-_/](?P<y>\d{2})(?!\d)"),
     "dmy2"),
)

#: A bare year the folder is already filed under adds nothing — the view puts it
#: in `by-event/2007/` and the label starts with `2007-`.
_TRAILING_YEAR = re.compile(r"[\s,\-_]*(?<!\d)(19|20)\d{2}\s*$")

_ISO = re.compile(r"^(19|20)\d{2}-\d{2}-\d{2}")


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def date_in_name(folder: str, year: str | None = None
                 ) -> tuple[datetime | None, str]:
    """→ ``(date written in the folder name, the name with it removed)``.

    Half of this library's hand-named folders carry their own date, in four
    different formats — `04.10.03`, `2008.08.12 rieusselat`,
    `02.07.2005 - fete de la musique 2005`, `perthus 02-08-07`. That is the
    operator's own writing about their own photographs, so it outranks anything
    inferred from EXIF; it is also the only date available for the 266 curated
    events whose contents are salvage with no readable timestamp at all.

    A date is accepted only if it is a real calendar date and, when the parent
    year is known, falls in it — which is what tells `06.04.07` (6 April 2007)
    apart from a phone number or a score line.
    """
    for pat, kind in _NAME_DATES:
        for m in pat.finditer(folder):
            y, mo, d = int(m.group("y")), int(m.group("m")), int(m.group("d"))
            if kind == "dmy2":
                y += 2000
            try:
                when = datetime(y, mo, d)
            except ValueError:
                continue
            if year and f"{when:%Y}" != str(year):
                continue
            rest = (folder[:m.start()] + " " + folder[m.end():])
            return when, _tidy(rest)
    return None, _tidy(folder)


def _tidy(name: str) -> str:
    """Trim the separators a removed date leaves behind, and capitalise once.

    Only the first letter is ever touched, and only upward: `alzon 2009` becomes
    `Alzon 2009`, while `chez adrien GIGNAC` keeps the surname the operator
    chose to shout. Nothing is lowercased and no word is otherwise rewritten.
    """
    name = re.sub(r"\s+", " ", name.replace("_", " ")).strip(" -–—,._/")
    if name and name[0].isalpha():
        name = name[0].upper() + name[1:]
    return name


def label_for_curated(folder: str, when: datetime | None,
                      year: str | None = None) -> str:
    """`2008-01-01 · Jour de lan 2008` — one format for the whole year folder.

    Every name gets the same ISO prefix so a year sorts chronologically, which
    is the entire reason to prefix at all. Previously 333 of 336 curated events
    got no prefix and the year folder was in four date formats at once.

    The date is taken from the folder's own name when it has one, and otherwise
    from the photographs; see `resolve_curated_date` for the ladder. A date that
    contradicts the parent year is never shown: photographs in
    `By Date/2008/Jour de lan 2008` carry dates as late as 2018 — rescans, or a
    date inherited from the wrong near-duplicate — and trusting their median
    produced `2018-05-09 · Jour de lan 2008` filed under 2008. The operator's
    own folder is better evidence of *when* than anything derived from its
    contents.
    """
    written, rest = date_in_name(folder, year)
    when = written or when
    if when is not None and year and f"{when:%Y}" != str(year):
        when = None
    if when is None:
        # Undatable — 239 of these are salvage whose only date is the parent
        # year, which the view folder already states. The name still loses that
        # redundant year so `Noel 2007` and `2007-06-21 · Fete de la musique`
        # read as the same kind of thing.
        return _drop_folder_year(rest or folder, year) or _tidy(folder)
    # A name that is *only* a date — `04.10.03` — becomes the prefix itself.
    if not rest:
        return f"{when:%Y-%m-%d}"
    # `alzon 2007` inside `by-event/2007/` behind a `2007-` prefix says 2007
    # three times. Strip it when it agrees; if that leaves nothing, the folder
    # was only ever the year and the date alone is the better name.
    if year and f"{when:%Y}" == str(year):
        stripped = _drop_folder_year(rest, year)
        if not stripped:
            return f"{when:%Y-%m-%d}"
        rest = stripped
    return f"{when:%Y-%m-%d}{_SEP}{_tidy(rest)}"


def _drop_folder_year(name: str, year: str | None) -> str:
    """Remove a trailing year that only repeats the folder the view puts it in.

    May return `""` when the name was nothing but that year; the caller decides
    what to do with a folder that has no words of its own.
    """
    tidy = _tidy(name)
    if not year:
        return tidy
    m = _TRAILING_YEAR.search(tidy)
    if m and m.group(0).strip(" ,-_") == str(year):
        return _tidy(tidy[:m.start()])
    return tidy


def label_for_detected(dates: list[datetime], hint: str, count: int = 0) -> str:
    """`2019-07-12..14 · Sète` — date first, because only it is measured.

    No photo count. It was there to show how big an occasion was, but the folder
    already shows that when opened, and `2007-01-09 (3)` reads as a name with a
    number stuck on rather than a name.
    """
    start, end = dates[0], dates[-1]
    span = f"{start:%Y-%m-%d}"
    if end.date() != start.date():
        span = (f"{span}..{end:%d}" if (end.year, end.month) == (start.year, start.month)
                else f"{span}..{end:%m-%d}")
    return f"{span}{_SEP}{hint}" if hint else span


def resolve_curated_date(strong: list[datetime], weak: list[datetime],
                         year: str) -> datetime | None:
    """When a hand-named folder happened, in descending order of evidence.

    1. the median of dates the ladder is confident about, if it falls in the year
       the folder is filed under;
    2. failing that, the median of *any* date in that year — the parent folder
       corroborates the year, so a folder-derived guess is good enough to order
       by even though it was never good enough to date a photo with.

    Step 2 is what the previous version was missing. It demanded confidence
    >= 0.80 for the prefix, and 266 of 336 curated events are salvage with no
    readable timestamp — so they got no prefix, and the year folder did not sort.
    """
    for pool in (strong, weak):
        inside = sorted(d for d in pool if f"{d:%Y}" == str(year))
        if inside:
            return inside[len(inside) // 2]
    return None


def _is_camera_model(leaf: str, models: set[str]) -> bool:
    """`Albums/General/Family/@family/Canon Powershot A2000` is a device, not a day.

    The library sorts some albums by the camera that took them, and those folder
    names were confidently offered as the name of an occasion — 59 events were
    called `Canon Powershot A95`. The set of real camera names is in the catalog
    already, so nothing has to be hardcoded or guessed.
    """
    low = leaf.casefold()
    return any(m and (m in low or low in m) for m in models)


#: Folder leaves that name a container rather than an occasion.
_NOT_AN_OCCASION = re.compile(
    r"^(?:\d+[\s._-]*\d*|photos?|images?|pictures?|divers|misc|new folder|dcim|"
    r"\d{3}[a-z]{4,7})$", re.IGNORECASE)


def _hint(conn, shas: list[str], camera_models: set[str]) -> str:
    """Best available description: where they were, who was there, what it was of.

    Each source is used only when one value dominates the event; a folder name is
    a bad place to assert something thin.
    """
    marks = ",".join("?" * len(shas))

    # A folder someone typed beats anything inferred, so it is consulted first —
    # a run inside `Albums/Trips/Malaga` is the Malaga trip whatever the pixels
    # say. Date buckets, device folders and generic containers are not names.
    folders = Counter()
    for r in conn.execute(
            f"SELECT rel FROM files WHERE sha256 IN ({marks}) AND present = 1", shas):
        parts = Path(r[0]).parts[:-1]
        if not parts or parts[0] in {"By Date", "_Recovered", "_Undated", "Camera"}:
            continue
        leaf = parts[-1]
        if (_DATE_BUCKET.fullmatch(leaf) or leaf.startswith(("#", "@"))
                or _NOT_AN_OCCASION.match(leaf)
                or _is_camera_model(leaf, camera_models)):
            continue
        folders[leaf] += 1
    if folders:
        top, n = folders.most_common(1)[0]
        if n >= max(3, len(shas) * 0.60):
            return _tidy(top)

    places = Counter(
        f"{r[0]}, {r[1]}" if r[1] else r[0]
        for r in conn.execute(
            f"SELECT place, country FROM geo WHERE sha256 IN ({marks}) AND place IS NOT NULL",
            shas))
    if places:
        top, n = places.most_common(1)[0]
        if n >= max(2, len(shas) * 0.30):
            return top

    people = Counter(
        r[0] for r in conn.execute(
            f"""SELECT p.name FROM faces f JOIN people p ON p.person_id = f.person_id
                WHERE f.sha256 IN ({marks}) AND p.name IS NOT NULL""", shas))
    if people:
        named = [n for n, c in people.most_common(2) if c >= 2]
        if named:
            return ", ".join(named)

    # Subjects come last and have to be nearly unanimous. `2019-02-12 · city`
    # names nothing — half this library is outdoors in a city — where
    # `2019-01-28 · sunset` does. The bar is what separates them.
    subjects = Counter(
        r[0] for r in conn.execute(
            f"SELECT subject FROM subjects WHERE sha256 IN ({marks})", shas))
    if subjects:
        top, n = subjects.most_common(1)[0]
        if n >= max(3, len(shas) * 0.50):
            return top
    return ""


def detect(root: Path, *, gap_hours: float = DEFAULT_GAP_HOURS,
           min_size: int = DEFAULT_MIN_SIZE,
           min_confidence: float = MIN_DATE_CONFIDENCE,
           only_classes: tuple[str, ...] = ("photo",), quiet: bool = False) -> dict:
    """Assign every photograph to an occasion. Curated names always win."""
    from . import catalog, dates as D  # noqa: PLC0415

    root = Path(root).resolve()
    conn = catalog.connect(root)
    camera_models = {(r[0] or "").casefold() for r in conn.execute(
        "SELECT DISTINCT p_camera FROM files WHERE p_camera IS NOT NULL "
        "AND p_camera <> ''")}

    # Every present file, with its class — NOT pre-filtered to photographs.
    #
    # A folder a human named is an occasion whatever is inside it. Filtering to
    # `photo` first silently dropped 40 curated events whose contents happen to
    # be webcam frames or unreadable salvage — 424 perfectly viewable files among
    # them. Curation outranks classification exactly as it outranks clustering.
    rows = conn.execute("""
        SELECT f.sha256, f.rel, d.value AS dv, d.confidence AS dc, d.source AS ds,
               COALESCE(a.decoded, 0) AS decoded,
               (SELECT class FROM classes c WHERE c.sha256 = f.sha256 LIMIT 1) AS cls
        FROM files f
        LEFT JOIN dates d ON d.sha256 = f.sha256
        LEFT JOIN assets a ON a.sha256 = f.sha256
        WHERE f.present = 1 AND f.root = ? AND f.sha256 IS NOT NULL
        GROUP BY f.sha256""", (str(root),)).fetchall()

    curated: dict[tuple[str, str], list[str]] = {}
    #: Curated members' dates, kept in two pools — see `resolve_curated_date`.
    cur_strong: dict[tuple[str, str], list[datetime]] = {}
    cur_weak: dict[tuple[str, str], list[datetime]] = {}
    #: How many members of each curated event can actually be opened.
    cur_viewable: Counter = Counter()
    loose: list[tuple[str, datetime]] = []
    undated = 0

    for r in rows:
        parts = Path(r["rel"]).parts
        any_when = _parse(r["dv"])
        # A year-only claim is stored as noon on 1 July and reads exactly like a
        # measured date. Displaying one as a day put fourteen 2007 occasions —
        # Airsoft, Alzon, Noel, Jour de lan — under the same `2007-07-01`.
        if any_when is not None and D.precision_of(r["ds"] or "", r["rel"]) != D.DAY:
            any_when = None
        when = any_when if (r["dc"] or 0) >= min_confidence else None
        if (len(parts) >= 4 and parts[0] == "By Date"
                and not _DATE_BUCKET.fullmatch(parts[2])):
            key = (parts[1], parts[2])
            curated.setdefault(key, []).append(r["sha256"])
            cur_viewable[key] += bool(r["decoded"])
            if when is not None:
                cur_strong.setdefault(key, []).append(when)
            elif any_when is not None:
                cur_weak.setdefault(key, []).append(any_when)
            continue
        # Detection, by contrast, only groups photographs: clustering screenshots
        # by timestamp would produce occasions nobody attended.
        if r["cls"] not in only_classes:
            continue
        if when is None:
            undated += 1
            continue
        loose.append((r["sha256"], when))

    out: list[tuple] = []

    # 1 — curation, untouched. A hand-made name outranks any clustering.
    unprefixed = 0
    for key, members in curated.items():
        year, folder = key
        when = resolve_curated_date(cur_strong.get(key, []), cur_weak.get(key, []), year)
        name = label_for_curated(folder, when, year)
        unprefixed += not _ISO.match(name)
        for sha in members:
            out.append((sha, year, f"{year}/{folder}", name, "folder"))

    # 2 — everything else, split on time gaps within each year.
    by_year: dict[str, list[tuple[str, datetime]]] = {}
    for sha, when in loose:
        by_year.setdefault(f"{when:%Y}", []).append((sha, when))

    detected = skipped = 0
    for year, items in by_year.items():
        items.sort(key=lambda t: t[1])
        run: list[tuple[str, datetime]] = []
        runs: list[list[tuple[str, datetime]]] = []
        for entry in items:
            if run and (entry[1] - run[-1][1]).total_seconds() >= gap_hours * 3600:
                runs.append(run)
                run = []
            run.append(entry)
        if run:
            runs.append(run)

        for i, r in enumerate(runs):
            if len(r) < min_size:
                skipped += len(r)
                continue
            shas = [s for s, _w in r]
            dates = [w for _s, w in r]
            name = label_for_detected(dates, _hint(conn, shas, camera_models))
            key = f"{year}/{dates[0]:%Y%m%d}-{i:04d}"
            for sha in shas:
                out.append((sha, year, key, name, "detected"))
            detected += 1

    out = _disambiguate(out)

    conn.execute("DELETE FROM events")
    conn.executemany(
        """INSERT OR REPLACE INTO events (sha256, year, event_key, event_name, source)
           VALUES (?,?,?,?,?)""", out)
    conn.commit()

    # An occasion whose every file is unreadable bytes gets no folder in the view,
    # because a view is for looking at things. Its NAME is still the only surviving
    # record of that afternoon, so the number is stated rather than left to be
    # discovered by comparing the catalog against the disk.
    unviewable = sum(1 for k in curated if not cur_viewable[k])

    stats = {"photos": len(out), "curated_events": len(curated),
             "detected_events": detected, "too_small": skipped,
             "undated": undated, "considered": len(rows), "unprefixed": unprefixed,
             "unviewable_events": unviewable}
    if not quiet:
        print(f"[events] {len(curated)} curated + {detected} detected "
              f"= {len(curated) + detected} occasions over {len(out):,} photographs",
              flush=True)
        print(f"[events] {unprefixed} curated names could not be dated and will sort "
              f"after the rest of their year", flush=True)
        if unviewable:
            print(f"[events] {unviewable} curated occasions have no readable file "
                  f"left and so get no folder — the name survives only in the catalog",
                  flush=True)
        # A view that quietly omits a third of the library is worse than one that
        # says what it cannot place.
        print(f"[events] {undated:,} photographs have no date confident enough to place "
              f"in time; {skipped:,} more fell in runs under {min_size}", flush=True)
    return stats


def _disambiguate(out: list[tuple]) -> list[tuple]:
    """Two occasions in one year may not claim the same folder name.

    Dropping the photo count from detected labels made collisions possible for
    the first time — two runs a year apart share `2019-05-04` only if they are in
    different years, but a curated `Noel` dated to the same day as a detected run
    would silently merge two occasions into one folder. Distinct events keep
    distinct folders; the discriminator is added only where one is needed, so the
    common case stays clean.
    """
    names: dict[tuple[str, str], set[str]] = {}
    for _sha, year, key, name, _src in out:
        names.setdefault((year, name), set()).add(key)
    renames: dict[str, str] = {}
    for (_year, name), keys in names.items():
        if len(keys) < 2:
            continue
        for n, key in enumerate(sorted(keys), start=1):
            renames[key] = f"{name} ({n})"
    if not renames:
        return out
    return [(sha, year, key, renames.get(key, name), src)
            for sha, year, key, name, src in out]
