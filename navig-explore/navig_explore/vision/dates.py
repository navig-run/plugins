"""The date ladder — recovering capture dates that EXIF no longer holds.

`explore photos organize` dates strictly from EXIF, and it is right to: guessing
a date and then filing a photo under it is corruption that looks like data. But
in salvage that rule leaves almost everything undated — in this library's
``_Recovered`` folder only 33 of 4,838 files still carry ``DateTimeOriginal``.

So instead of guessing, this module *derives* dates from evidence and records
which evidence it used. Every rung writes ``source`` and ``confidence``; nothing
downstream is allowed to treat an inferred date as a measured one, and
``photos explain`` prints the whole derivation for any file.

The rungs, strongest first:

1. ``exif``      -- DateTimeOriginal / CreateDate, as probed.
2. ``overlay``   -- OCR of a burned-in webcam timestamp. Worth more than its
                    position suggests: 1,548 files here are webcam frames whose
                    EXIF is gone but whose date is printed in the pixels.
3. ``filename``  -- camera and messenger naming conventions.
4. ``twin``      -- a near-duplicate that *is* dated, in an already-organised
                    part of the library, lends its date and event name.
5. ``folder``    -- a year parsed out of a human-named event folder.
6. ``sibling``   -- the median date of the folder's other files.
7. ``mtime``     -- last, and only when the mtime is not demonstrably junk.

Rung 7 is where recovery libraries are usually destroyed. A carving tool stamps
every file it writes with the time of the *recovery run*, so thousands of photos
share one meaningless timestamp — 4,797 files here all claim 2010. The junk
detector finds those by frequency and refuses them, which is the single highest-
yield rule in this module.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# Confidence is ordinal, not probabilistic: it ranks evidence and gates moves.
CONFIDENCE = {
    "exif": 1.0,
    "overlay": 0.95,
    "filename": 0.90,
    "twin": 0.80,
    "folder": 0.60,
    "sibling": 0.50,
    "mtime": 0.30,
}

MIN_YEAR, MAX_YEAR = 1990, datetime.now().year + 1

# ── month tokens, across scripts and OCR confusions ─────────────────────────
_RU = ["янв", "фев", "мар", "апр", "май", "июн",
       "июл", "авг", "сен", "окт", "ноя", "дек"]
_EN = ["jan", "feb", "mar", "apr", "may", "jun",
       "jul", "aug", "sep", "oct", "nov", "dec"]
# Tesseract routinely returns Latin lookalikes for Cyrillic glyphs — "мар" comes
# back as "map", "апр" as "anp" (Cyrillic п reads as Latin n). Transliterating the
# genuinely confusable glyphs gives us those spellings without hand-listing every
# OCR mangling. Deliberately excludes г and б: mapping г→r would turn "авг"
# (August) into "abr", which fuzzy-matches "apr" — silently shifting a date by
# four months.
_CONFUSABLE = str.maketrans("авекмнопрстух", "abekmhonpctyx")

# Spellings the transliteration cannot reach. Registered explicitly rather than
# left to fuzzy matching, because the failure mode of a near-miss is not "no
# date" — it is a confidently wrong month.
_OCR_VARIANTS = {
    "yanv": 1, "janv": 1, "fev": 2, "feb": 2, "mart": 3,
    "app": 4, "aprl": 4, "mai": 5, "iyun": 6, "iyul": 7, "abg": 8, "avg": 8,
    "sent": 9, "okt": 10, "noyb": 11, "dek": 12, "dekb": 12,
}

_MONTHS: dict[str, int] = {}
for _i, (_ru, _en) in enumerate(zip(_RU, _EN), start=1):
    _MONTHS[_ru] = _i
    _MONTHS[_en] = _i
    _MONTHS[_ru.translate(_CONFUSABLE)] = _i
_MONTHS.update({k.lower(): v for k, v in _OCR_VARIANTS.items()})


def month_from_token(tok: str) -> int | None:
    """Map a (possibly OCR-mangled) month abbreviation to 1-12.

    NFC, not NFKD: compatibility decomposition splits Cyrillic ``й`` into ``и``
    plus a combining breve, so "май" stopped matching its own dictionary entry
    and fuzzy-matched to "map" — May silently became March.
    """
    t = unicodedata.normalize("NFC", tok).lower().strip(" .,:")
    # Never truncate a long word down to something month-shaped. "sensor" — which
    # appears in every one of this library's webcam overlays — otherwise clipped
    # to "sen" and fuzzy-matched to September.
    if not t or len(t) > 4:
        return None
    if t in _MONTHS:
        return _MONTHS[t]
    t3 = t[:3]
    if t3 in _MONTHS:
        return _MONTHS[t3]
    close = difflib.get_close_matches(t3, list(_MONTHS), n=1, cutoff=0.6)
    return _MONTHS[close[0]] if close else None


def _valid(y: int, mo: int, d: int, h: int = 12, mi: int = 0, s: int = 0):
    if not (MIN_YEAR <= y <= MAX_YEAR and 1 <= mo <= 12 and 1 <= d <= 31):
        return None
    try:
        return datetime(y, mo, d, min(h, 23), min(mi, 59), min(s, 59))
    except ValueError:
        return None


# ── rung 2: burned-in overlay ───────────────────────────────────────────────
# "мар 28, 2005 03:37 sensor:18"  ·  "dec 09, 2005 21:08"
_OVERLAY_RE = re.compile(
    r"([A-Za-zА-Яа-я]{3,4})\s*[.,]?\s*(\d{1,2})\s*[.,]\s*(\d{4})"
    r"(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?")
# "2005-03-28 03:37:11" / "2005/03/28 03:37"
_OVERLAY_ISO_RE = re.compile(
    r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?")
# "28.03.2005 03:37" (day-first, the common European camera overlay)
_OVERLAY_DMY_RE = re.compile(
    r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?")


def parse_overlay(text: str) -> datetime | None:
    """Pull a timestamp out of OCR'd overlay text."""
    if not text:
        return None
    flat = " ".join(text.split())

    m = _OVERLAY_RE.search(flat)
    if m:
        mo = month_from_token(m.group(1))
        if mo:
            dt = _valid(int(m.group(3)), mo, int(m.group(2)),
                        int(m.group(4) or 12), int(m.group(5) or 0), int(m.group(6) or 0))
            if dt:
                return dt

    m = _OVERLAY_ISO_RE.search(flat)
    if m:
        dt = _valid(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                    int(m.group(4) or 12), int(m.group(5) or 0), int(m.group(6) or 0))
        if dt:
            return dt

    m = _OVERLAY_DMY_RE.search(flat)
    if m:
        dt = _valid(int(m.group(3)), int(m.group(2)), int(m.group(1)),
                    int(m.group(4) or 12), int(m.group(5) or 0), int(m.group(6) or 0))
        if dt:
            return dt
    return None


def ocr_overlay(path: str, *, lang: str = "rus+eng") -> tuple[datetime | None, str]:
    """OCR the top and bottom bands of a frame, looking for a timestamp.

    Both bands, because overlay position is a camera setting, not a standard.
    The band is upscaled 4x first: the text is only a few pixels tall at 320x240
    and Tesseract reads almost nothing at native size.
    """
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    import pytesseract  # noqa: PLC0415

    try:
        bgr = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception:  # noqa: BLE001
        return None, ""
    if bgr is None:
        return None, ""

    h = bgr.shape[0]
    band_h = max(14, int(h * 0.07))
    seen = []
    for band in (bgr[0:band_h, :], bgr[h - band_h:h, :]):
        try:
            big = cv2.resize(band, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
            txt = pytesseract.image_to_string(big, lang=lang, config="--psm 7")
        except Exception:  # noqa: BLE001 - missing tesseract must not kill the run
            return None, ""
        seen.append(txt.strip())
        dt = parse_overlay(txt)
        if dt:
            return dt, txt.strip()
    return None, " | ".join(s for s in seen if s)


# ── rung 3: filenames ───────────────────────────────────────────────────────
_FILENAME_PATTERNS = [
    # IMG_20180712_143012 · PXL_20180712_143012345 · VID_20180712
    re.compile(r"(?:IMG|PXL|VID|MVIMG|DSC|PANO|SAVE)[-_]?(\d{4})(\d{2})(\d{2})"
               r"(?:[-_]?(\d{2})(\d{2})(\d{2}))?", re.I),
    # WhatsApp: IMG-20180712-WA0001
    re.compile(r"IMG-(\d{4})(\d{2})(\d{2})-WA\d+", re.I),
    # Screenshot 2019-03-04 at 11.22.33 · 2019-03-04_11-22-33
    re.compile(r"(\d{4})[-_.](\d{2})[-_.](\d{2})(?:[ _tT]+(\d{2})[-_.:](\d{2})"
               r"(?:[-_.:](\d{2}))?)?"),
    # 20180712_143012
    re.compile(r"\b(\d{4})(\d{2})(\d{2})[-_](\d{2})(\d{2})(\d{2})\b"),
    # photo_22@26-04-2026_14-07-45  (day-first, Telegram-style)
    re.compile(r"@(\d{2})-(\d{2})-(\d{4})[-_](\d{2})-(\d{2})-(\d{2})"),
]


def from_filename(name: str) -> datetime | None:
    stem = Path(name).stem
    for i, rx in enumerate(_FILENAME_PATTERNS):
        m = rx.search(stem)
        if not m:
            continue
        g = [int(x) if x else 0 for x in m.groups()]
        if i == 4:  # day-first variant
            d, mo, y, hh, mi, ss = g[0], g[1], g[2], g[3], g[4], g[5]
        else:
            y, mo, d = g[0], g[1], g[2]
            hh, mi, ss = (g + [0, 0, 0])[3:6]
        dt = _valid(y, mo, d, hh or 12, mi, ss)
        if dt:
            return dt
    return None


# ── rung 5: folder names ────────────────────────────────────────────────────
_YEAR_RE = re.compile(r"(?<!\d)(19[89]\d|20[0-4]\d)(?!\d)")
_DDMMYY_RE = re.compile(r"(?<!\d)(\d{2})\.(\d{2})\.(\d{2})(?!\d)")


def from_folder(rel: str) -> tuple[datetime | None, str, str]:
    """Year (or dd.mm.yy day) parsed from any component of the path.

    The ~300 human-named event folders in this library ("Alzon 2009",
    "14 juillet 2009", "Берлин 2014") make this unusually productive — and it is
    a signal no off-the-shelf photo manager looks at.

    Returns the **precision** alongside, because a year-only claim is stored as
    a real datetime and is otherwise indistinguishable from a real one. A folder
    called `Alzon 2007` yields `2007-07-01T12:00:00` — noon on the first of July,
    a date nothing happened on. Reading that back as a day put fourteen different
    2007 occasions under the same fabricated `2007-07-01` prefix. Anything that
    displays a stored date has to know how much of it was measured.
    """
    parts = list(Path(rel).parts[:-1])
    for part in reversed(parts):
        m = _DDMMYY_RE.search(part)
        if m:
            d, mo, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
            year = 2000 + yy if yy < 50 else 1900 + yy
            dt = _valid(year, mo, d)
            if dt:
                return dt, part, DAY
        m = _YEAR_RE.search(part)
        if m:
            dt = _valid(int(m.group(1)), 7, 1)  # mid-year: a year-only claim
            if dt:
                return dt, part, YEAR
    return None, "", YEAR


#: How much of a stored date was actually measured.
DAY, YEAR = "day", "year"


def precision_of(source: str, rel: str) -> str:
    """How precisely a stored date pins a photo down.

    Only `folder` is ever coarse; every other rung reads a real timestamp or a
    written-out date. For `folder` the path itself says which branch produced
    the value, so this is pure string work and needs no extra column.
    """
    if source != "folder":
        return DAY
    return from_folder(rel)[2]


# ── rung 7: the junk-mtime detector ─────────────────────────────────────────
def junk_minutes(conn, root: Path, *, per_minute: int = 90) -> set[int]:
    """Minutes into which implausibly many files were written.

    Bucketing by *second* — the obvious approach — misses real carving runs
    entirely: PhotoRec spreads its output over many seconds, so the biggest
    single-second bucket in this library holds only 49 files and looks ordinary.
    At minute resolution the same run is unmistakable: 569 files in one minute.
    No camera sustains 1.5 shots per second for sixty seconds; a disk writer does.
    """
    rows = conn.execute(
        "SELECT mtime FROM files WHERE present=1 AND root=?",
        (str(Path(root).resolve()),)).fetchall()
    if not rows:
        return set()
    counts = Counter(int(r["mtime"]) // 60 for r in rows if r["mtime"])
    return {m for m, n in counts.items() if n >= per_minute}


def mtime_is_trustworthy(conn, root: Path, *, min_sample: int = 20,
                         min_agreement: float = 0.25,
                         tolerance_days: int = 2) -> tuple[bool, str]:
    """Decide whether mtime is evidence *at all* for this library.

    Rather than guessing, this asks the files that already have a measured date
    (EXIF, overlay OCR, filename) whether their mtime agrees with it. If almost
    none do, the filesystem clock is recording when the recovery tool ran, not
    when the shutter fired, and the whole rung is disabled for this root.

    Self-validating, and it needs no knowledge of which carving tool was used.
    """
    rows = conn.execute(
        """SELECT f.mtime, d.value FROM files f
           JOIN dates d ON d.sha256 = f.sha256
           WHERE f.present=1 AND f.root=? AND d.confidence >= 0.9 AND f.mtime > 0""",
        (str(Path(root).resolve()),)).fetchall()
    if len(rows) < min_sample:
        return True, f"only {len(rows)} measured dates — not enough to judge, allowing mtime"

    agree = 0
    for r in rows:
        try:
            measured = datetime.fromisoformat(r["value"])
            fs = datetime.fromtimestamp(int(r["mtime"]))
        except (ValueError, OSError, OverflowError):
            continue
        if abs((fs - measured).days) <= tolerance_days:
            agree += 1
    ratio = agree / len(rows)
    if ratio < min_agreement:
        return False, (f"mtime disagrees with {len(rows) - agree} of {len(rows)} "
                       f"measured dates ({ratio:.0%} agreement) — treating the "
                       f"filesystem clock as recovery-run noise")
    return True, f"mtime agrees with {ratio:.0%} of measured dates"


# ── rung 4: twin inheritance ────────────────────────────────────────────────
def _phash_to_int(h: str) -> int | None:
    try:
        return int(h, 16)
    except (TypeError, ValueError):
        return None


def parse_exif(value: str) -> datetime | None:
    """ExifTool renders capture time as ``2005:03:28 03:37:11``."""
    if not value:
        return None
    v = str(value).strip().replace("/", ":")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S", "%Y:%m:%d"):
        try:
            dt = datetime.strptime(v[:len(datetime.now().strftime(fmt))], fmt)
        except ValueError:
            continue
        if MIN_YEAR <= dt.year <= MAX_YEAR:
            return dt
    return None


def find_twins(conn, root: Path, *, max_distance: int = 8, chunk: int = 512,
               min_donor_confidence: float = 0.9) -> dict[str, tuple[str, int]]:
    """Undated sha256 -> (donor sha256, hamming distance) via pHash.

    Donors are restricted to *measured* dates (EXIF, overlay OCR, filename) so
    an inferred date can never become the evidence for another inference — a
    chain of guesses looks exactly like a fact by the third link.

    Note on safety: this library's rule is that near-image matches must never
    drive deletion, because most of them are burst frames rather than copies.
    That rule is about *deleting*. Burst frames are seconds apart, so for
    *dating* they are not a hazard but an asset — the same evidence, used for a
    non-destructive purpose.
    """
    import numpy as np  # noqa: PLC0415

    popcount = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)

    donors, undated = [], []
    for r in conn.execute(
        """SELECT a.sha256, a.phash, d.value, d.confidence FROM assets a
           LEFT JOIN dates d ON d.sha256 = a.sha256
           WHERE a.phash IS NOT NULL AND a.decoded = 1"""
    ):
        v = _phash_to_int(r["phash"])
        if v is None:
            continue
        if r["value"] and (r["confidence"] or 0) >= min_donor_confidence:
            donors.append((r["sha256"], v))
        elif not r["value"]:
            undated.append((r["sha256"], v))

    if not donors or not undated:
        return {}

    d_keys = [s for s, _ in donors]
    d_vals = np.array([v for _, v in donors], dtype=np.uint64)
    out: dict[str, tuple[str, int]] = {}

    for start in range(0, len(undated), chunk):
        block = undated[start:start + chunk]
        u_vals = np.array([v for _, v in block], dtype=np.uint64)
        # Vectorised Hamming distance: XOR, then popcount each byte via a table.
        xor = np.ascontiguousarray(np.bitwise_xor(u_vals[:, None], d_vals[None, :]))
        dist = popcount[xor.view(np.uint8).reshape(xor.shape[0], xor.shape[1], 8)
                        ].sum(axis=-1)
        best = dist.argmin(axis=1)
        for i, (sha, _) in enumerate(block):
            dd = int(dist[i, best[i]])
            if dd <= max_distance:
                out[sha] = (d_keys[int(best[i])], dd)
    return out


# ── orchestration ───────────────────────────────────────────────────────────
def _has_date(conn, sha: str) -> bool:
    return conn.execute("SELECT 1 FROM dates WHERE sha256=?", (sha,)).fetchone() is not None


def _store(conn, rows: list[tuple]) -> None:
    """Write dates, but only ever *upward* in confidence."""
    if not rows:
        return
    conn.executemany(
        """INSERT INTO dates (sha256, value, source, confidence, detail)
           VALUES (?,?,?,?,?)
           ON CONFLICT(sha256) DO UPDATE SET
             value=excluded.value, source=excluded.source,
             confidence=excluded.confidence, detail=excluded.detail
           WHERE excluded.confidence > dates.confidence""",
        rows)
    conn.commit()


def _ocr_pass(conn, todo: list[dict], workers: int, quiet: bool) -> int:
    import queue  # noqa: PLC0415
    import threading  # noqa: PLC0415

    if not todo:
        return 0
    if not quiet:
        print(f"[dates] OCR overlay on {len(todo)} webcam frames", flush=True)
    out_q: queue.Queue = queue.Queue()
    src, lock = iter(todo), threading.Lock()

    def _worker():
        while True:
            with lock:
                item = next(src, None)
            if item is None:
                break
            dt, txt = ocr_overlay(item["path"])
            out_q.put((item["sha256"], dt, txt))
        out_q.put(None)

    pool = [threading.Thread(target=_worker, daemon=True) for _ in range(max(1, workers))]
    for t in pool:
        t.start()

    done, found, batch = 0, 0, []
    while done < len(pool):
        item = out_q.get()
        if item is None:
            done += 1
            continue
        sha, dt, txt = item
        if dt:
            batch.append((sha, dt.isoformat(timespec="seconds"), "overlay",
                          CONFIDENCE["overlay"], txt[:120]))
        if len(batch) >= 200:
            _store(conn, batch)
            found += len(batch)
            batch = []
    _store(conn, batch)
    return found + len(batch)


def resolve(root: Path, *, do_ocr: bool = True, ocr_workers: int = 8,
            twin_distance: int = 8, sibling_max_folder: int = 400,
            sibling_max_spread_days: int = 31, quiet: bool = False) -> dict:
    """Run the whole ladder over a library and record dates with provenance.

    Purely an indexing operation: it writes to ``vision.db`` and never touches a
    photo. Rungs run strongest-first and a stronger source always wins, so this
    is idempotent and safe to re-run as new evidence appears.
    """
    from . import catalog  # noqa: PLC0415

    root = Path(root).resolve()
    conn = catalog.connect(root)
    stats: dict = defaultdict(int)

    rows = conn.execute(
        """SELECT f.sha256, f.rel, f.name, f.mtime, f.p_created, f.path,
                  (SELECT class FROM classes c WHERE c.sha256=f.sha256 LIMIT 1) AS cls
           FROM files f
           WHERE f.present=1 AND f.root=? AND f.sha256 IS NOT NULL""",
        (str(root),)).fetchall()
    if not quiet:
        print(f"[dates] {len(rows)} files", flush=True)

    # rung 1 — EXIF
    batch = []
    for r in rows:
        dt = parse_exif(r["p_created"])
        if dt:
            batch.append((r["sha256"], dt.isoformat(timespec="seconds"), "exif",
                          CONFIDENCE["exif"], "DateTimeOriginal"))
    _store(conn, batch)
    stats["exif"] = len(batch)

    # rung 2 — burned-in overlay, webcam frames only
    if do_ocr:
        todo = [dict(r) for r in rows
                if r["cls"] == "webcam" and not _has_date(conn, r["sha256"])]
        stats["overlay"] = _ocr_pass(conn, todo, ocr_workers, quiet)

    # rung 3 — filename
    batch = []
    for r in rows:
        dt = from_filename(r["name"])
        if dt:
            batch.append((r["sha256"], dt.isoformat(timespec="seconds"), "filename",
                          CONFIDENCE["filename"], r["name"]))
    _store(conn, batch)
    stats["filename"] = len(batch)

    # rung 4 — twin inheritance, donors restricted to measured dates
    twins = find_twins(conn, root, max_distance=twin_distance)
    if twins:
        donor = {r["sha256"]: r["value"]
                 for r in conn.execute("SELECT sha256, value FROM dates")}
        drel = {r["sha256"]: r["rel"] for r in conn.execute(
            "SELECT sha256, rel FROM files WHERE sha256 IS NOT NULL")}
        batch = [(sha, donor[d], "twin", CONFIDENCE["twin"],
                  f"pHash d={dist} from {drel.get(d, d)[:90]}")
                 for sha, (d, dist) in twins.items() if d in donor]
        _store(conn, batch)
        stats["twin"] = len(batch)

    # rung 5 — year from a human-named event folder
    batch = []
    for r in rows:
        dt, part, _precision = from_folder(r["rel"])
        if dt:
            batch.append((r["sha256"], dt.isoformat(timespec="seconds"), "folder",
                          CONFIDENCE["folder"], part))
    _store(conn, batch)
    stats["folder"] = len(batch)

    # rung 6 — sibling median, ONLY inside a temporally coherent folder.
    #
    # Both guards below are load-bearing. Without them this rung dated 3,893 of
    # 4,812 files in one go: `_Recovered` is a single flat folder, so every
    # undated file inherited the median of thousands of unrelated photos spanning
    # 2000-2012. "Everything in this folder was shot around the same time" is only
    # true of an event folder, and has to be checked rather than assumed.
    by_dir: dict = defaultdict(list)
    for r in rows:
        by_dir[str(Path(r["rel"]).parent)].append(r["sha256"])
    known = {r["sha256"]: r["value"] for r in conn.execute(
        "SELECT sha256, value FROM dates WHERE confidence >= 0.9")}
    batch = []
    for _d, shas in by_dir.items():
        if len(shas) > sibling_max_folder:
            stats["sibling_folder_too_big"] += 1
            continue
        vals = sorted(known[s] for s in shas if s in known)
        if len(vals) < 3:
            continue
        try:
            spread = (datetime.fromisoformat(vals[-1]) - datetime.fromisoformat(vals[0])).days
        except ValueError:
            continue
        if spread > sibling_max_spread_days:
            stats["sibling_folder_incoherent"] += 1
            continue
        median = vals[len(vals) // 2]
        for s in shas:
            if s not in known:
                batch.append((s, median, "sibling", CONFIDENCE["sibling"],
                              f"median of {len(vals)} dated files spanning {spread}d"))
    _store(conn, batch)
    stats["sibling"] = len(batch)

    # rung 7 — mtime, but only if this library's mtimes are evidence at all
    trusted, why = mtime_is_trustworthy(conn, root)
    stats["mtime_trusted"] = int(trusted)
    stats["mtime_verdict"] = why
    if not quiet:
        print(f"[dates] mtime check: {why}", flush=True)
    if trusted:
        junk = junk_minutes(conn, root)
        stats["junk_minute_buckets"] = len(junk)
        batch = []
        for r in rows:
            ts = int(r["mtime"] or 0)
            if not ts or (ts // 60) in junk:
                stats["mtime_rejected"] += 1
                continue
            try:
                dt = datetime.fromtimestamp(ts)
            except (OSError, ValueError, OverflowError):
                continue
            if MIN_YEAR <= dt.year <= MAX_YEAR:
                batch.append((r["sha256"], dt.isoformat(timespec="seconds"), "mtime",
                              CONFIDENCE["mtime"], "filesystem mtime"))
        _store(conn, batch)
        stats["mtime"] = len(batch)
    else:
        stats["mtime_rejected"] = len(rows)

    final = {r["source"]: r["n"] for r in conn.execute(
        "SELECT source, COUNT(*) n FROM dates GROUP BY source")}
    stats["total_dated"] = sum(final.values())
    stats["by_source"] = final
    if not quiet:
        print(f"[dates] dated {stats['total_dated']} files: {final}", flush=True)
        print(f"[dates] refused {stats['mtime_rejected']} junk mtimes across "
              f"{len(junk)} poisoned timestamp values", flush=True)
    return dict(stats)


def explain(root: Path, path: str) -> dict:
    """Everything the catalog knows about one file, and how it was derived."""
    import os  # noqa: PLC0415

    from . import catalog  # noqa: PLC0415

    root = Path(root).resolve()
    conn = catalog.connect(root)
    key = os.path.normcase(os.path.abspath(path))
    f = conn.execute("SELECT * FROM files WHERE path=?", (key,)).fetchone()
    if f is None:
        return {}
    sha = f["sha256"]
    out: dict = {"path": f["path"], "rel": f["rel"], "sha256": sha,
                 "size": f["size"], "exif_created": f["p_created"],
                 "camera": f["p_camera"]}
    if not sha:
        return out
    a = conn.execute("SELECT * FROM assets WHERE sha256=?", (sha,)).fetchone()
    if a:
        out["asset"] = {k: a[k] for k in a.keys()}
    d = conn.execute("SELECT * FROM dates WHERE sha256=?", (sha,)).fetchone()
    if d:
        out["date"] = {"value": d["value"], "source": d["source"],
                       "confidence": d["confidence"], "detail": d["detail"]}
    out["classes"] = [(r["class"], r["score"]) for r in conn.execute(
        "SELECT class, score FROM classes WHERE sha256=? ORDER BY score DESC", (sha,))]
    out["faces"] = [{"score": r["score"], "rotation": r["rotation"], "person": r["name"]}
                    for r in conn.execute(
                        """SELECT f.score, f.rotation, p.name FROM faces f
                           LEFT JOIN people p ON p.person_id=f.person_id
                           WHERE f.sha256=?""", (sha,))]
    g = conn.execute("SELECT * FROM geo WHERE sha256=?", (sha,)).fetchone()
    if g:
        out["geo"] = {"lat": g["lat"], "lon": g["lon"], "place": g["place"],
                      "source": g["source"], "confidence": g["confidence"]}
    out["also_at"] = [r["path"] for r in conn.execute(
        "SELECT path FROM files WHERE sha256=? AND path<>?", (sha, key))]
    return out
