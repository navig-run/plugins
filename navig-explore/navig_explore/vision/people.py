"""Grouping faces into people, and keeping the names you assign.

Two rules shape this module, both learned from how face clustering fails in
practice rather than how it works in papers:

**Re-clustering must not renumber people.** HDBSCAN is unsupervised, so a second
run over a slightly larger set can shuffle every cluster id. If names are stored
against cluster ids, an hour of naming evaporates on the next scan. So a name is
stored against a ``people`` row with a frozen centroid, the row is marked
``locked``, and subsequent runs *assign* faces to locked people by centroid
distance instead of re-deriving them.

**Clustering always over-splits.** The same person at 15 and at 35, in glasses,
in profile, at 320x240, will land in several clusters. That is not a bug to tune
away — merge and split are first-class operations here, because a human deciding
"these two are the same person" is information no threshold can derive.
"""
from __future__ import annotations

from pathlib import Path

# Minimum cosine SIMILARITY for a face to join a person.
#
# Calibrated against this library's own faces rather than guessed. Over 64,446
# within-person and 20,000 cross-person pairs from SFace embeddings:
#
#   within-person   p5 0.339 · median 0.553 · p75 0.641
#   cross-person    median 0.098 · p95 0.253 · p99 0.325 · max 0.547
#
#   threshold   recall   false-merge
#     0.363      92.9%      0.36%     <- OpenCV's documented SFace value
#     0.400      88.3%      0.13%     <- chosen: 3x safer for 5% less recall
#     0.620      30.3%      0.00%     <- what this used to be, as a DISTANCE of 0.38
#
# The old value captured barely a third of genuine same-person pairs, which is
# why two thirds of all faces ended up unclustered and nothing ever re-attached
# to a named person. A false merge is still the worse error — it is invisible in
# the result — so this sits above the vendor default rather than below it.
ASSIGN_MIN_SIMILARITY = 0.40

# Clearing the bar is not enough — the match must also be UNAMBIGUOUS.
#
# Adopting on "nearest centroid above threshold" alone took coverage from 33% to
# 95%, and visibly wrecked the biggest groups: one held a woman, two men, a
# monkey mask and a cartoon emoji. A face that sits between two people is not
# evidence about either of them, so require the best match to beat the runner-up
# by this margin. Faces that stay ambiguous are left unassigned, which is the
# honest outcome — an unassigned face is visible, a wrongly-filed one is not.
ASSIGN_MIN_MARGIN = 0.08


def _vectors(conn, engine: str | None = None):
    import numpy as np  # noqa: PLC0415

    from . import catalog  # noqa: PLC0415

    sql = "SELECT face_id, sha256, vec, dim FROM faces WHERE vec IS NOT NULL"
    args: tuple = ()
    if engine:
        sql += " AND engine = ?"
        args = (engine,)
    ids, shas, rows = [], [], []
    for r in conn.execute(sql, args):
        ids.append(r["face_id"])
        shas.append(r["sha256"])
        rows.append(catalog.unpack_vec(r["vec"]))
    if not rows:
        return ids, shas, np.zeros((0, 0), dtype="float32")
    return ids, shas, np.vstack(rows).astype("float32")


def cluster(root: Path, *, engine: str | None = None, min_cluster_size: int = 3,
            adopt: bool = True, quiet: bool = False) -> dict:
    """Bootstrap clusters with HDBSCAN, then respect every locked person."""
    import numpy as np  # noqa: PLC0415

    from . import catalog  # noqa: PLC0415

    root = Path(root).resolve()
    conn = catalog.connect(root)
    ids, _shas, mat = _vectors(conn, engine)
    if len(ids) == 0:
        return {"faces": 0, "clusters": 0, "assigned_to_named": 0,
                "adopted": 0, "noise": 0}

    try:
        from sklearn.cluster import HDBSCAN  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Face clustering needs scikit-learn:  py -3.13 -m pip install scikit-learn"
        ) from exc

    # Vectors are unit-length, so euclidean here is a monotone function of cosine.
    labels = HDBSCAN(min_cluster_size=min_cluster_size, metric="euclidean",
                     store_centers="centroid", copy=True).fit_predict(mat)
    conn.executemany("UPDATE faces SET cluster_id=? WHERE face_id=?",
                     [(int(lbl), int(fid)) for fid, lbl in zip(ids, labels)])
    conn.commit()

    # Re-attach to people the operator has already named.
    locked = [(r["person_id"], catalog.unpack_vec(r["centroid"]))
              for r in conn.execute(
                  "SELECT person_id, centroid FROM people "
                  "WHERE locked=1 AND centroid IS NOT NULL")]
    assigned = 0
    if locked:
        # ONLY faces this run left unclustered may be claimed by a named person.
        #
        # Comparing every face against the locked centroids — which is what this
        # did — lets a named person overwrite membership HDBSCAN had already
        # decided correctly. At the old, far-too-strict threshold it never fired,
        # so the bug was invisible; at a working threshold one group cascaded to
        # 12,840 photos while the next largest held 497. A group 26x bigger than
        # the runner-up is not a well-photographed person, it is a merge bug.
        free = [i for i, lbl in enumerate(labels) if int(lbl) < 0]
        if free:
            cen = np.vstack([c for _, c in locked])
            cen = cen / np.linalg.norm(cen, axis=1, keepdims=True)
            sub = mat[free]
            sims = sub @ cen.T
            best = sims.argmax(axis=1)
            upd = [(int(locked[int(best[k])][0]), int(ids[i]))
                   for k, i in enumerate(free)
                   if float(sims[k, best[k]]) >= ASSIGN_MIN_SIMILARITY]
            if upd:
                conn.executemany("UPDATE faces SET person_id=? WHERE face_id=?", upd)
                conn.commit()
                assigned = len(upd)

    # Every unnamed cluster gets an unnamed person row, so it is addressable.
    made = 0
    for lbl in sorted({int(x) for x in labels if int(x) >= 0}):
        row = conn.execute(
            """SELECT COUNT(*) n FROM faces
               WHERE cluster_id=? AND person_id IS NULL""", (lbl,)).fetchone()
        if not row["n"]:
            continue
        vecs = np.vstack([catalog.unpack_vec(r["vec"]) for r in conn.execute(
            "SELECT vec FROM faces WHERE cluster_id=? AND person_id IS NULL", (lbl,))])
        centroid = vecs.mean(axis=0)
        cur = conn.execute(
            "INSERT INTO people (name, locked, dim, centroid, n_faces) VALUES (?,?,?,?,?)",
            (None, 0, int(vecs.shape[1]), catalog.pack_vec(centroid), int(vecs.shape[0])))
        conn.execute("UPDATE faces SET person_id=? WHERE cluster_id=? AND person_id IS NULL",
                     (cur.lastrowid, lbl))
        made += 1
    conn.commit()

    adopted = adopt_unclustered(conn, quiet=quiet) if adopt else 0

    noise = conn.execute(
        "SELECT COUNT(*) FROM faces WHERE person_id IS NULL AND vec IS NOT NULL"
    ).fetchone()[0]
    warning = check_for_cascade(conn)
    if warning and not quiet:
        print(f"[people] ⚠ {warning}", flush=True)
    if not quiet:
        print(f"[people] {len(ids)} faces → {made} new groups, "
              f"{assigned} re-attached to named people, {adopted} adopted into an "
              f"existing group, {noise} still unclustered", flush=True)
    return {"faces": len(ids), "clusters": made, "assigned_to_named": assigned,
            "adopted": adopted, "noise": noise}


# A real person can be the most photographed in a library by some margin. They
# cannot plausibly be an order of magnitude ahead of everyone else — that shape
# means one group has been absorbing strangers.
CASCADE_RATIO = 8.0


def check_for_cascade(conn) -> str | None:
    """Warn when one group is implausibly larger than the rest.

    A false merge is the dangerous failure here because it is invisible: the
    folder still looks like a person, just with more photos in it. Comparing the
    biggest group to the runner-up makes the shape of that failure loud.
    """
    rows = conn.execute("""
        SELECT person_id, COUNT(*) n FROM faces WHERE person_id IS NOT NULL
        GROUP BY person_id ORDER BY n DESC LIMIT 2""").fetchall()
    if len(rows) < 2 or rows[1]["n"] == 0:
        return None
    ratio = rows[0]["n"] / rows[1]["n"]
    if ratio < CASCADE_RATIO:
        return None
    return (f"group {rows[0]['person_id']} holds {rows[0]['n']:,} faces — "
            f"{ratio:.0f}x the next largest ({rows[1]['n']:,}). That is the shape of a "
            f"merge cascade, not a well-photographed person. Inspect it before "
            f"trusting the grouping, and `people split` it if it is wrong.")


def adopt_unclustered(conn, *, min_similarity: float = ASSIGN_MIN_SIMILARITY,
                      min_margin: float = ASSIGN_MIN_MARGIN,
                      chunk: int = 4096, quiet: bool = False) -> int:
    """Give every unclustered face the nearest group it clearly belongs to.

    HDBSCAN is deliberately conservative: it labels anything in a sparse region
    as noise, which on a 20-year library of varied lighting, resolution and age
    left **two thirds of all faces in no group at all**. Those are not strangers,
    they are ordinary photos of the same people in harder conditions.

    A second pass fixes it without loosening the clustering itself: compare each
    orphan to the established group centroids and adopt it only when the match
    clears the calibrated threshold. Clusters stay tight; coverage stops being
    hostage to density.
    """
    import numpy as np  # noqa: PLC0415

    from . import catalog  # noqa: PLC0415

    cents, pids = [], []
    for r in conn.execute("""
            SELECT person_id, centroid, dim FROM people
            WHERE centroid IS NOT NULL"""):
        v = catalog.unpack_vec(r["centroid"])
        n = np.linalg.norm(v)
        if n:
            cents.append(v / n)
            pids.append(r["person_id"])
    if not cents:
        return 0
    C = np.vstack(cents).astype("float32")

    orphans = conn.execute(
        "SELECT face_id, vec FROM faces WHERE person_id IS NULL AND vec IS NOT NULL"
    ).fetchall()
    if not orphans:
        return 0

    total = 0
    for start in range(0, len(orphans), chunk):
        block = orphans[start:start + chunk]
        M = np.vstack([catalog.unpack_vec(r["vec"]) for r in block]).astype("float32")
        M /= np.linalg.norm(M, axis=1, keepdims=True)
        sims = M @ C.T
        best = sims.argmax(axis=1)
        top = sims[np.arange(len(block)), best]
        if C.shape[0] > 1:
            # Second-best per row, for the ambiguity test.
            masked = sims.copy()
            masked[np.arange(len(block)), best] = -1.0
            runner_up = masked.max(axis=1)
        else:
            runner_up = np.full(len(block), -1.0, dtype="float32")
        upd = [(int(pids[int(best[i])]), int(r["face_id"]))
               for i, r in enumerate(block)
               if float(top[i]) >= min_similarity
               and float(top[i] - runner_up[i]) >= min_margin]
        if upd:
            conn.executemany("UPDATE faces SET person_id=? WHERE face_id=?", upd)
            total += len(upd)
    conn.commit()

    # Centroids shift once a group absorbs new members; keep them honest.
    # One pass over all faces, grouped in memory — a query per group was 3,491
    # round-trips and turned a minutes-long step into a run that timed out.
    acc: dict[int, list] = {}
    for r in conn.execute(
            "SELECT person_id, vec FROM faces "
            "WHERE person_id IS NOT NULL AND vec IS NOT NULL"):
        acc.setdefault(r["person_id"], []).append(catalog.unpack_vec(r["vec"]))
    conn.executemany(
        "UPDATE people SET centroid=?, n_faces=? WHERE person_id=?",
        [(catalog.pack_vec(np.vstack(v).mean(axis=0)), len(v), pid)
         for pid, v in acc.items()])
    conn.commit()
    if not quiet:
        print(f"[people] adopted {total} previously unclustered faces", flush=True)
    return total


def listing(root: Path, *, limit: int = 40) -> list[dict]:
    from . import catalog  # noqa: PLC0415

    conn = catalog.connect(Path(root).resolve())
    return [dict(r) for r in conn.execute(
        """SELECT p.person_id, p.name, p.locked, COUNT(f.face_id) n
           FROM people p LEFT JOIN faces f ON f.person_id = p.person_id
           GROUP BY p.person_id ORDER BY n DESC LIMIT ?""", (limit,))]


#: Centroid cosine above which two groups are worth *looking at* as one person.
#:
#: Deliberately a suggestion, never an action. Measured across this library's
#: 60 largest groups the closest pair sits at 0.795 and most fall under 0.55, so
#: there is no value that separates "same person, split by the clusterer" from
#: "brother and sister" — only a human looking at both can say. Merging on a
#: number is how one group swallowed 12,840 faces.
MERGE_SUGGEST_SIMILARITY = 0.50


def merge_candidates(root: Path, *, top: int = 60, min_photos: int = 10,
                     min_similarity: float = MERGE_SUGGEST_SIMILARITY) -> list[dict]:
    """Pairs of groups close enough to be worth eyeballing side by side.

    Clustering always over-splits one person into several groups; this finds the
    pairs to check without asserting any of them are the same.
    """
    import numpy as np  # noqa: PLC0415

    from . import catalog  # noqa: PLC0415

    conn = catalog.connect(Path(root).resolve())
    rows = conn.execute("""
        SELECT p.person_id, p.name, COUNT(DISTINCT f.sha256) photos
        FROM people p JOIN faces f ON f.person_id = p.person_id
        GROUP BY p.person_id HAVING photos >= ?
        ORDER BY photos DESC LIMIT ?""", (min_photos, top)).fetchall()
    cents, meta = [], []
    for r in rows:
        vecs = [catalog.unpack_vec(v["vec"]) for v in conn.execute(
            "SELECT vec FROM faces WHERE person_id=? AND vec IS NOT NULL",
            (r["person_id"],))]
        if not vecs:
            continue
        c = np.vstack(vecs).mean(axis=0)
        cents.append(c / (np.linalg.norm(c) or 1.0))
        meta.append(r)
    if len(cents) < 2:
        return []
    sim = np.stack(cents) @ np.stack(cents).T
    np.fill_diagonal(sim, -1.0)
    out = []
    for i in range(len(meta)):
        for j in range(i + 1, len(meta)):
            if sim[i, j] >= min_similarity:
                out.append({"a": meta[i]["person_id"], "b": meta[j]["person_id"],
                            "a_photos": meta[i]["photos"], "b_photos": meta[j]["photos"],
                            "a_name": meta[i]["name"], "b_name": meta[j]["name"],
                            "similarity": float(sim[i, j])})
    return sorted(out, key=lambda d: -d["similarity"])


def contact_sheet(root: Path, out: Path, *, limit: int = 40, per_group: int = 8,
                  thumb: int = 128, min_photos: int = 5) -> dict:
    """One row of faces per group, labelled with the id `people name` takes.

    `people list` prints ids and counts, which says nothing about who anyone is —
    so 3,491 groups stayed anonymous and `by-person` filled with `person-<id>`
    folders. Naming needs seeing, and a face crop is the smallest thing that
    can be seen.

    Faces are sampled ACROSS the group rather than taken from the front: the
    first few are near-identical frames from one burst, which shows the operator
    nothing about whether the group holds one person or three.
    """
    from PIL import Image, ImageDraw  # noqa: PLC0415

    from . import catalog  # noqa: PLC0415

    root = Path(root).resolve()
    conn = catalog.connect(root)
    groups = conn.execute("""
        SELECT p.person_id, p.name, COUNT(DISTINCT f.sha256) photos
        FROM people p JOIN faces f ON f.person_id = p.person_id
        GROUP BY p.person_id HAVING photos >= ?
        ORDER BY photos DESC LIMIT ?""", (min_photos, limit)).fetchall()
    if not groups:
        return {"groups": 0, "faces": 0, "out": None}

    label_w, pad = 190, 4
    width = label_w + per_group * (thumb + pad)
    height = 12 + len(groups) * (thumb + pad)
    canvas = Image.new("RGB", (width, height), "#111")
    draw = ImageDraw.Draw(canvas)
    drawn = 0

    for gi, g in enumerate(groups):
        faces = conn.execute("""
            SELECT f.x, f.y, f.w, f.h, f.rotation, fi.path
            FROM faces f JOIN files fi ON fi.sha256 = f.sha256
            WHERE f.person_id = ? AND fi.present = 1
            GROUP BY f.face_id""", (g["person_id"],)).fetchall()
        step = max(1, len(faces) // per_group)
        picks = faces[::step][:per_group]
        y = 12 + gi * (thumb + pad)
        draw.text((6, y + thumb // 2 - 12),
                  f"id {g['person_id']}", fill="#eee")
        draw.text((6, y + thumb // 2 + 2),
                  f"{g['photos']} photos  {g['name'] or ''}"[:24], fill="#9cf")
        for k, f in enumerate(picks):
            x = label_w + k * (thumb + pad)
            try:
                im = Image.open(f["path"]).convert("RGB")
                if f["rotation"]:
                    im = im.rotate(-f["rotation"], expand=True)
                W, H = im.size
                # Boxes are stored as fractions, so they survive any resize.
                cx, cy = (f["x"] + f["w"] / 2) * W, (f["y"] + f["h"] / 2) * H
                half = max(f["w"] * W, f["h"] * H) * 0.75
                im = im.crop((int(cx - half), int(cy - half),
                              int(cx + half), int(cy + half))).resize((thumb, thumb))
                canvas.paste(im, (x, y))
                drawn += 1
            except Exception:  # noqa: BLE001 - one unreadable face is not a failure
                draw.rectangle([x, y, x + thumb, y + thumb], outline="#533")

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, quality=88)
    return {"groups": len(groups), "faces": drawn, "out": str(out)}


def name(root: Path, person_id: int, label: str) -> int:
    """Name a group and lock it, freezing its centroid against re-clustering."""
    import numpy as np  # noqa: PLC0415

    from . import catalog  # noqa: PLC0415

    conn = catalog.connect(Path(root).resolve())
    vecs = [catalog.unpack_vec(r["vec"]) for r in conn.execute(
        "SELECT vec FROM faces WHERE person_id=? AND vec IS NOT NULL", (person_id,))]
    if not vecs:
        raise ValueError(f"no faces for person {person_id}")
    centroid = np.vstack(vecs).mean(axis=0)
    conn.execute(
        "UPDATE people SET name=?, locked=1, centroid=?, n_faces=? WHERE person_id=?",
        (label, catalog.pack_vec(centroid), len(vecs), person_id))
    conn.commit()
    return len(vecs)


def merge(root: Path, keep: int, *others: int) -> int:
    """Fold groups together — the fix for the over-splitting that always happens."""
    import numpy as np  # noqa: PLC0415

    from . import catalog  # noqa: PLC0415

    conn = catalog.connect(Path(root).resolve())
    moved = 0
    for other in others:
        cur = conn.execute("UPDATE faces SET person_id=? WHERE person_id=?", (keep, other))
        moved += cur.rowcount
        conn.execute("DELETE FROM people WHERE person_id=?", (other,))
    vecs = [catalog.unpack_vec(r["vec"]) for r in conn.execute(
        "SELECT vec FROM faces WHERE person_id=? AND vec IS NOT NULL", (keep,))]
    if vecs:
        conn.execute("UPDATE people SET centroid=?, n_faces=? WHERE person_id=?",
                     (catalog.pack_vec(np.vstack(vecs).mean(axis=0)), len(vecs), keep))
    conn.commit()
    return moved


def split(root: Path, person_id: int) -> int:
    """Detach a group so the next `cluster` run can re-derive it."""
    from . import catalog  # noqa: PLC0415

    conn = catalog.connect(Path(root).resolve())
    cur = conn.execute("UPDATE faces SET person_id=NULL WHERE person_id=?", (person_id,))
    conn.execute("DELETE FROM people WHERE person_id=?", (person_id,))
    conn.commit()
    return cur.rowcount
