"""File metadata beats a zero-shot guess — use it first.

The CLIP classifier decides by argmax over five prompt sets, with no notion of
"I don't know". Measured against the real 97,600-photo library that produced:

* a **webcam** class of 15,797 files of which **7,421 carry camera EXIF** — a
  webcam frame cannot have a Canon or iPhone Make/Model. Its commonest
  resolutions were 2592x1944, 4032x3024 and 6000x4000: camera sensors. Only
  1,983 had a readable burned-in timestamp.
* a **screenshot** class holding 774 files at 4032x3024 — the iPhone camera
  sensor size — while 5,812 files at 1170x2532 (the iPhone 13/14 Pro *screen*)
  sat there with **zero** camera EXIF, unmistakable and unused.

So the metadata already knew. This module applies what is certain before the
guess is allowed to speak, and records `classes.source` so a rule-decided label
is never confused with a guessed one.

The rules mostly **exclude**: they say what a file cannot be. CLIP still arbitrates
photo-versus-document, where pixels genuinely are the evidence.
"""
from __future__ import annotations

from pathlib import Path

#: Exact phone/tablet screen sizes. A file at one of these, with no camera EXIF,
#: is a screen capture — verified on this library at 0 camera-EXIF out of 10,716.
PHONE_SCREENS: frozenset[tuple[int, int]] = frozenset({
    (320, 480), (640, 960), (640, 1136), (750, 1334), (828, 1792),
    (1080, 1920), (1080, 2340), (1125, 2436), (1170, 2532), (1179, 2556),
    (1242, 2208), (1242, 2688), (1284, 2778), (1290, 2796), (1440, 2560),
    (1440, 3200), (1125, 2000),
    # tablets
    (768, 1024), (1536, 2048), (1620, 2160), (1668, 2388), (2048, 2732),
})

#: Desktop capture sizes. Weaker evidence — 1920x1080 is also a video frame — so
#: these additionally require a lossless format and no camera EXIF.
DESKTOP_SCREENS: frozenset[tuple[int, int]] = frozenset({
    (1280, 800), (1366, 768), (1440, 900), (1680, 1050), (1920, 1080),
    (2560, 1440), (2560, 1600), (3440, 1440), (3840, 2160),
})

#: Analogue-era webcam/CCTV frame sizes (QVGA, CIF, VGA).
WEBCAM_SIZES: frozenset[tuple[int, int]] = frozenset({
    (320, 240), (352, 288), (640, 480), (384, 288), (176, 144),
})

LOSSLESS = frozenset({".png", ".bmp", ".webp"})

#: Which device a screen size belongs to. Once a file is known to be a screenshot
#: it has, by definition, no camera EXIF — so the *resolution* is the only thing
#: left that can answer "is this from my iPhone?", and it answers it exactly.
IPHONE_SCREENS: frozenset[tuple[int, int]] = frozenset({
    (320, 480), (640, 960), (640, 1136), (750, 1334), (828, 1792),
    (1125, 2436), (1170, 2532), (1179, 2556), (1242, 2208), (1242, 2688),
    (1284, 2778), (1290, 2796), (1125, 2000),
})
IPAD_SCREENS: frozenset[tuple[int, int]] = frozenset({
    (768, 1024), (1536, 2048), (1620, 2160), (1640, 2360), (1668, 2388),
    (2048, 2732),
})
#: Portrait phone sizes that are not distinctively Apple. 1080x1920 is claimed by
#: both Android handsets and a downscaled iPhone 6 Plus, so the bucket is named
#: for what is actually known rather than guessing a vendor.
OTHER_PHONE_SCREENS: frozenset[tuple[int, int]] = frozenset({
    (1080, 1920), (1080, 2340), (1080, 2400), (1440, 2560), (1440, 3200),
})


def device_for(w: int, h: int, *, camera: str = "") -> str:
    """Which device produced a screen capture, from its exact resolution.

    Orientation is checked before the size tables, because several sizes are a
    phone one way up and a monitor the other: 1440x2560 portrait is a handset,
    2560x1440 landscape is a desktop display.
    """
    w, h = w or 0, h or 0
    portrait = (w, h) if h >= w else (h, w)
    landscape = (w, h) if w >= h else (h, w)

    # A landscape frame at an exact monitor size is a desktop capture, even when
    # its portrait form collides with a phone.
    if w > h and landscape in DESKTOP_SCREENS:
        return "desktop"
    if portrait in IPHONE_SCREENS:
        return "iPhone"
    if portrait in IPAD_SCREENS:
        return "iPad"
    if portrait in OTHER_PHONE_SCREENS:
        return "other phone"
    if landscape in DESKTOP_SCREENS:
        return "desktop"

    cam = (camera or "").strip().lower()
    if "iphone" in cam:
        return "iPhone"
    if "ipad" in cam:
        return "iPad"
    return "other"


def _oriented(w: int, h: int) -> tuple[tuple[int, int], tuple[int, int]]:
    return (w, h), (h, w)


def classify_one(*, w: int, h: int, ext: str, camera: str,
                 has_overlay_date: bool) -> tuple[str, str] | None:
    """→ ``(class, rule)`` when metadata settles it, else ``None``.

    Order matters: the strongest evidence is consulted first.
    """
    ext = (ext or "").lower()
    camera = (camera or "").strip()
    a, b = _oriented(w, h)

    # A burned-in timestamp that actually parsed is proof, not inference.
    if has_overlay_date:
        return "webcam", "rule:overlay-timestamp"

    # A real camera wrote this file, so it is a photograph or a photo OF
    # something — certainly not a screen capture and not a webcam frame. That is
    # expressed as an exclusion via `forbidden_for`, leaving CLIP to choose
    # between photo and document, where the pixels genuinely are the evidence.
    if camera:
        return None

    # No camera, and exactly a device screen size — but orientation decides how
    # much that is worth. Portrait 1080x1920 is 809 files here with zero camera
    # EXIF: phone screenshots. LANDSCAPE 1920x1080 is ordinary Full-HD photos and
    # video frames, and matching it swept them all in. So the portrait form is
    # accepted outright and the landscape form has to be lossless as well.
    if a in PHONE_SCREENS:
        return "screenshot", "rule:phone-screen-size"
    if b in PHONE_SCREENS and ext in LOSSLESS:
        return "screenshot", "rule:phone-screen-size-landscape"
    if (a in DESKTOP_SCREENS or b in DESKTOP_SCREENS) and ext in LOSSLESS:
        return "screenshot", "rule:desktop-screen-size"

    # Small, no camera, not a screen size — the shape of an old webcam frame.
    if a in WEBCAM_SIZES or b in WEBCAM_SIZES:
        return "webcam", "rule:webcam-frame-size"
    return None


#: A webcam/CCTV frame from this era is small. Anything with a long side beyond
#: this is a camera photo, whatever the pixels suggest — the leftover mistakes in
#: the webcam folder were a hand holding a device and a cracked phone screen,
#: both multi-megapixel.
WEBCAM_MAX_LONG_SIDE = 1280

#: Above this, a file a camera wrote is a photograph of something, not clipart.
#:
#: `web-graphic` means a banner, logo, meme or box art. A downloaded picture that
#: really is a photograph belongs in `photo` whoever took it. Leaving the class
#: available to multi-megapixel camera files made it the runner-up in 4,864
#: `unsorted` ties — by far the largest single cause of that bucket.
WEB_GRAPHIC_MAX_LONG_SIDE = 1280


def forbidden_for(*, camera: str, w: int = 0, h: int = 0) -> frozenset[str]:
    """Classes a file provably is NOT, from metadata alone."""
    banned: set[str] = set()
    long_side = max(w or 0, h or 0)
    if (camera or "").strip():
        # 7,421 files with camera EXIF were sitting in the webcam class.
        banned |= {"screenshot", "webcam"}
        if long_side > WEB_GRAPHIC_MAX_LONG_SIDE:
            banned.add("web-graphic")
    if long_side > WEBCAM_MAX_LONG_SIDE:
        banned.add("webcam")
    return frozenset(banned)


FALLBACK = "unsorted"


#: How sure the winning class must be against its runner-up, at the model's own
#: temperature — see `classify.confident`.
#:
#: Its predecessor was an absolute similarity margin of 0.010, which sounds tiny
#: and is not: the whole top1-to-top2 gap distribution here has a median of
#: 0.0219, so that bar rejected **24.5%** of the library. `unsorted` reached
#: 10,833 files — 3,131 of them carrying camera EXIF, 1,071 at 6000x4000, i.e.
#: unmistakable photographs the year and event views then never showed.
#:
#: 0.65 pairwise reproduces the full-softmax 0.55 this replaced (unsorted 5,704
#: vs 5,156, photo 62,207 vs 62,630 — 0.7%) while no longer depending on how many
#: classes happen to exist.
MIN_CLASS_PROBABILITY = 0.65


def refine(root: Path, *, min_probability: float = MIN_CLASS_PROBABILITY,
           quiet: bool = False) -> dict:
    """Re-decide every file's class: rules first, then the guess, then `unsorted`.

    ``min_probability`` is how sure the best CLIP class must be to be trusted at
    all. Below it the honest label is `unsorted` — a visible bucket the operator
    can look at — rather than a confident-looking wrong folder.
    """
    from . import catalog, classify, embed as E  # noqa: PLC0415

    root = Path(root).resolve()
    conn = catalog.connect(root)
    model = E.model_id()

    overlay = {r["sha256"] for r in conn.execute(
        "SELECT sha256 FROM dates WHERE source = 'overlay'")}
    meta = {r["sha256"]: r for r in conn.execute("""
        SELECT f.sha256, f.ext, f.p_camera AS camera, a.w, a.h
        FROM files f JOIN assets a ON a.sha256 = f.sha256
        WHERE f.present = 1 AND a.decoded = 1 AND f.sha256 IS NOT NULL""")}

    keys, mat = catalog.load_matrix(conn, model)
    classes, sims = ([], None)
    if len(keys):
        classes, sims = classify.score(mat)

    stats: dict[str, int] = {}
    rows: list[tuple] = []
    for i, sha in enumerate(keys):
        m = meta.get(sha)
        if m is None:
            continue
        decided = classify_one(w=m["w"] or 0, h=m["h"] or 0, ext=m["ext"],
                               camera=m["camera"], has_overlay_date=sha in overlay)
        if decided:
            label, rule = decided
        else:
            banned = forbidden_for(camera=m["camera"], w=m["w"] or 0, h=m["h"] or 0)
            allowed = [j for j in range(len(classes)) if classes[j] not in banned]
            # The softmax runs over the ALLOWED classes only. Including a class
            # the metadata has already ruled out would let it drain probability
            # from the real answer and manufacture an ambiguity that is not there.
            pick, _p = classify.confident(sims[i][allowed], bar=min_probability)
            if pick is None:
                label, rule = FALLBACK, "clip:ambiguous"
            else:
                label = classes[allowed[pick]]
                rule = "clip" if not banned else "clip:camera-excluded"
        rows.append((sha, label, float(sims[i].max()), rule))
        stats[label] = stats.get(label, 0) + 1
        stats[f"via {rule}"] = stats.get(f"via {rule}", 0) + 1

    conn.execute("DELETE FROM classes")
    conn.executemany(
        "INSERT OR REPLACE INTO classes (sha256, class, score, source) VALUES (?,?,?,?)",
        rows)
    conn.commit()
    if not quiet:
        print(f"[classes] re-decided {len(rows):,} files", flush=True)
        for k, v in sorted(stats.items(), key=lambda kv: -kv[1]):
            print(f"    {v:>7,}  {k}", flush=True)
    return stats
