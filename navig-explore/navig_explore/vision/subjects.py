"""What a photograph is *of* — sunsets, food, animals, the sea.

Deliberately different in shape from :mod:`classify`. A file is exactly one of
photo/screenshot/webcam, so that is argmax. But a photograph of dinner on a
terrace at sunset is **food and sunset and architecture at once**, and forcing a
single winner throws away most of what makes the library searchable. Subjects are
therefore multi-label: every subject clearing its own bar is kept.

Two properties matter more than the taxonomy itself:

**Absolute similarity does not transfer between prompts.** SigLIP scores sit in a
narrow band and each prompt set has its own centre — "a sunset" is simply a
higher-scoring phrase than "a document on a desk". A single global threshold
therefore over-fires on some subjects and never fires on others. The bar is set
per subject from the library's own score distribution instead.

**Nothing is forced.** A photo matching no subject confidently gets none. That
was the lesson from the content classes, where argmax with no "I don't know"
filled folders with confident nonsense.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

#: group -> subject -> prompts. The group only shapes the folder tree.
TAXONOMY: dict[str, dict[str, list[str]]] = {
    "nature": {
        "sunset": ["a sunset", "a sunrise with an orange sky",
                   "the sun setting over the horizon", "golden hour sky"],
        "sea": ["the sea", "an ocean view", "waves on a beach", "a seaside coastline"],
        "beach": ["a sandy beach", "people on a beach", "a beach with parasols"],
        "mountains": ["a mountain range", "a view from a mountain summit",
                      "snow-capped peaks", "a valley between mountains"],
        "forest": ["a forest", "trees in the woods", "a woodland path"],
        "lake-river": ["a lake", "a river", "a waterfall", "a reflection on still water"],
        "snow": ["a snowy landscape", "snow covering the ground", "a winter scene with snow"],
        "flowers": ["flowers in bloom", "a bouquet of flowers", "a flower close-up"],
        "sky-clouds": ["dramatic clouds in the sky", "a blue sky with clouds",
                       "a storm cloud"],
        "night-sky": ["a starry night sky", "the moon at night", "the milky way"],
        "garden": ["a garden", "a park with grass and trees", "a green lawn"],
    },
    "life": {
        "portrait": ["a portrait of one person", "a close-up photograph of a face",
                     "a headshot of a person"],
        "group": ["a group of people together", "friends posing for a photo",
                  "a family group photograph"],
        "children": ["a small child", "a baby", "children playing"],
        "wedding": ["a wedding", "a bride and groom", "a wedding ceremony"],
        "party": ["a party with people celebrating", "a birthday party with a cake",
                  "people dancing at a nightclub"],
        "concert": ["a concert stage with lights", "a live band performing",
                    "a crowd at a music festival"],
        "sport": ["people playing sport", "a football match", "a runner or cyclist",
                  "a ski slope with skiers"],
        "selfie": ["a selfie taken at arm's length", "a mirror selfie"],
    },
    "things": {
        "food": ["a plate of food", "a meal on a table", "a dish in a restaurant",
                 "a cake or dessert"],
        "drinks": ["a glass of wine", "a cup of coffee", "cocktails on a bar",
                   "bottles of beer"],
        "car": ["a car", "a parked automobile", "a car interior from the driver seat"],
        "motorcycle": ["a motorcycle", "a scooter"],
        "boat": ["a boat", "a sailing yacht", "a harbour with boats"],
        "aircraft": ["an aeroplane", "an aircraft in flight", "an airport terminal"],
        "train": ["a train", "a railway station platform"],
        "bicycle": ["a bicycle"],
        "computer": ["a computer screen and keyboard on a desk", "a laptop"],
        "phone": ["a mobile phone held in a hand"],
        "musical-instrument": ["a guitar", "a piano", "a drum kit",
                               "someone playing a musical instrument"],
        "artwork": ["a painting", "a drawing or sketch", "street art or graffiti",
                    "a sculpture"],
        "books": ["books on a shelf", "an open book", "a library"],
    },
    "places": {
        "city": ["a city street", "a cityscape skyline", "buildings in a town centre"],
        "architecture": ["a church or cathedral", "a castle", "a monument",
                         "an ornate building facade"],
        "interior": ["the inside of a room", "a living room", "a kitchen interior"],
        "restaurant-cafe": ["the inside of a restaurant", "a cafe interior",
                            "a bar counter"],
        "shop": ["a shop interior", "a supermarket aisle", "a market stall"],
        "museum": ["a museum exhibition", "an art gallery wall"],
    },
    "animals": {
        "cat": ["a cat"],
        "dog": ["a dog"],
        "bird": ["a bird"],
        "horse": ["a horse"],
        "wildlife": ["a wild animal", "an animal at a zoo", "a farm animal"],
    },
}

#: How far above a subject's own baseline a score must sit to count. Expressed in
#: standard deviations of that subject's distribution across the library, so it
#: adapts to prompts that simply score higher than others.
DEFAULT_SIGMA = 2.6

#: …and how far above the photo's OWN average across all subjects.
#:
#: The two bars answer different questions and neither works alone:
#:
#: * **column** (``DEFAULT_SIGMA``) — is this photo unusual *for this subject*,
#:   compared with every other photo? Handles prompt sets that simply score
#:   higher: `sunset` bottoms out at 0.119 where `books` tops out at 0.125.
#: * **row** (this) — is this subject unusual *for this photo*, compared with the
#:   other 42? Handles the failure the column bar cannot see at all.
#:
#: That failure is what put 292 photos in `books`, of which the tail was a tree
#: mural, a pergola, a blurred hand and several near-black rooms. A dark, blurred
#: or otherwise low-information frame sits near the centre of embedding space and
#: scores middling on *everything*; nothing in a per-subject distribution
#: distinguishes "confidently a bookshelf" from "confidently nothing at all". Its
#: score profile across subjects is flat, and that is directly measurable.
#: Requiring both bars took `books` 292 -> 30 and made its tail bookshelves.
DEFAULT_ROW_SIGMA = 3.0

#: Optional last-resort ceiling, off by default.
#:
#: This was once ``0.005`` and load-bearing, which was the bug: the percentile it
#: implies sat *above* the sigma bar for 41 of 43 subjects, so every subject was
#: admitted at exactly 294 files — the cap had quietly become the only rule, and
#: it asserts that a library holds as many books as sunsets. With the row bar in
#: place the counts come from the data instead (city 900, flowers 757, books 30,
#: sport 12), which is the shape of a real photo library.
DEFAULT_TOP_FRACTION = 0.0

#: Never keep more than this many subjects for one photo — beyond it the labels
#: stop meaning anything, whatever the scores say.
MAX_PER_PHOTO = 4


def subjects() -> list[tuple[str, str]]:
    """[(group, subject), …] in taxonomy order."""
    return [(g, s) for g, subs in TAXONOMY.items() for s in subs]


def _cache_path(model_id: str) -> Path:
    import os  # noqa: PLC0415

    key = hashlib.sha1(  # noqa: S324 - cache key, not a security boundary
        (model_id + json.dumps(TAXONOMY, sort_keys=True)).encode()).hexdigest()[:16]
    d = Path(os.environ.get("NAVIG_HOME", Path.home() / ".navig")) / "cache" / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"subject-prototypes-{key}.npy"


def prototypes(*, model: str | None = None, pretrained: str | None = None):
    """(pairs, matrix) of unit vectors, one row per subject. Cached on disk."""
    import numpy as np  # noqa: PLC0415

    from . import embed as E  # noqa: PLC0415

    pairs = subjects()
    mid = E.model_id(model or E.DEFAULT_MODEL, pretrained or E.DEFAULT_PRETRAINED)
    cache = _cache_path(mid)
    if cache.exists():
        return pairs, np.load(cache)

    mats = []
    for group, sub in pairs:
        vecs = E.embed_text(TAXONOMY[group][sub], model=model or E.DEFAULT_MODEL,
                            pretrained=pretrained or E.DEFAULT_PRETRAINED)
        v = vecs.mean(axis=0)
        mats.append(v / np.linalg.norm(v))
    mat = np.stack(mats).astype("float32")
    np.save(cache, mat)
    return pairs, mat


def tag(root: Path, *, sigma: float = DEFAULT_SIGMA,
        row_sigma: float = DEFAULT_ROW_SIGMA, max_per_photo: int = MAX_PER_PHOTO,
        top_fraction: float = DEFAULT_TOP_FRACTION,
        only_classes: tuple[str, ...] = ("photo",), quiet: bool = False) -> dict:
    """Attach subjects to every photograph, multi-label, with per-subject bars.

    Costs one matrix product against embeddings already in the catalog.
    """
    import numpy as np  # noqa: PLC0415

    from . import catalog, embed as E  # noqa: PLC0415

    root = Path(root).resolve()
    conn = catalog.connect(root)
    _ensure_table(conn)

    marks = ",".join("?" * len(only_classes))
    wanted = {r["sha256"] for r in conn.execute(
        f"SELECT sha256 FROM classes WHERE class IN ({marks})", only_classes)}
    if not wanted:
        return {"photos": 0, "tags": 0}

    keys, mat = catalog.load_matrix(conn, E.model_id())
    if not keys:
        raise RuntimeError("no embeddings — run `photos index` first")
    idx = [i for i, k in enumerate(keys) if k in wanted]
    if not idx:
        return {"photos": 0, "tags": 0}
    sub = mat[idx]

    pairs, protos = prototypes()
    sims = sub @ protos.T                      # (photos, subjects)

    # Normalise along BOTH axes; see the constants above for why one is not enough.
    #
    # Down each column: how this photo scores for this subject against every other
    # photo. Across each row: how this subject scores for this photo against the
    # other 42. A label has to clear both — unusual for the subject, and the
    # subject unusual for it.
    col = (sims - sims.mean(axis=0)) / np.maximum(sims.std(axis=0), 1e-6)
    row = ((sims - sims.mean(axis=1, keepdims=True))
           / np.maximum(sims.std(axis=1, keepdims=True), 1e-6))
    keep = (col >= sigma) & (row >= row_sigma)

    # A ceiling, if one is asked for. Deliberately not on by default — as a
    # *primary* bar this is what pinned every subject to the same count.
    if 0 < top_fraction < 1:
        pct = np.quantile(sims, 1.0 - top_fraction, axis=0)
        keep &= sims >= pct

    rows: list[tuple] = []
    per_subject: dict[str, int] = {}
    tagged_photos = 0
    for r, i in enumerate(idx):
        over = np.where(keep[r])[0]
        if len(over) == 0:
            continue
        over = over[np.argsort(-row[r][over])][:max_per_photo]
        tagged_photos += 1
        for j in over:
            group, name = pairs[int(j)]
            rows.append((keys[i], group, name, float(sims[r][int(j)])))
            per_subject[name] = per_subject.get(name, 0) + 1

    conn.execute("DELETE FROM subjects")
    conn.executemany(
        "INSERT OR REPLACE INTO subjects (sha256, grp, subject, score) VALUES (?,?,?,?)",
        rows)
    conn.commit()

    if not quiet:
        print(f"[subjects] {tagged_photos:,} of {len(idx):,} photographs tagged, "
              f"{len(rows):,} labels", flush=True)
        for name, n in sorted(per_subject.items(), key=lambda kv: -kv[1])[:20]:
            print(f"    {n:>7,}  {name}", flush=True)
    return {"photos": tagged_photos, "considered": len(idx), "tags": len(rows),
            "per_subject": per_subject}


def _ensure_table(conn) -> None:
    """Kept for callers that hold a bare connection; the schema owns the table now."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subjects (
            sha256  TEXT NOT NULL,
            grp     TEXT NOT NULL,
            subject TEXT NOT NULL,
            score   REAL NOT NULL,
            PRIMARY KEY (sha256, subject)
        )""")
    conn.commit()
