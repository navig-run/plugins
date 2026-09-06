"""Natural-language and image-similarity search over the catalog.

Exact brute-force cosine, not an approximate index. At ~100k photos the whole
embedding matrix is a ~100 MB float32 array and one matmul answers a query in
single-digit milliseconds, so an ANN index would add a dependency, a build step
and a staleness problem to buy nothing.

Queries run through SigLIP 2's text tower on the CPU by default: a query costs
~5 ms there and never waits on a CUDA context or competes with an indexing run
for VRAM.
"""
from __future__ import annotations

from pathlib import Path


def _filters(conn, *, person: str | None, year: int | None, place: str | None,
             klass: str | None, min_confidence: float | None) -> set[str] | None:
    """Intersect the structured filters into a sha256 allow-list (None = no filter)."""
    allow: set[str] | None = None

    def _and(new: set[str]) -> set[str]:
        return new if allow is None else (allow & new)

    if person:
        rows = conn.execute(
            """SELECT DISTINCT f.sha256 FROM faces f
               JOIN people p ON p.person_id = f.person_id
               WHERE p.name IS NOT NULL AND lower(p.name) LIKE lower(?)""",
            (f"%{person}%",))
        allow = _and({r["sha256"] for r in rows})
    if year:
        rows = conn.execute(
            "SELECT sha256 FROM dates WHERE value LIKE ?" +
            (" AND confidence >= ?" if min_confidence else ""),
            (f"{year}-%",) + ((min_confidence,) if min_confidence else ()))
        allow = _and({r["sha256"] for r in rows})
    if place:
        rows = conn.execute(
            """SELECT sha256 FROM geo
               WHERE lower(COALESCE(place,'') || ' ' || COALESCE(admin1,'') || ' '
                           || COALESCE(country,'')) LIKE lower(?)""",
            (f"%{place}%",))
        allow = _and({r["sha256"] for r in rows})
    if klass:
        rows = conn.execute("SELECT sha256 FROM classes WHERE class = ?", (klass,))
        allow = _and({r["sha256"] for r in rows})
    return allow


def search(root: Path, query: str | None = None, *, limit: int = 40,
           person: str | None = None, year: int | None = None,
           place: str | None = None, klass: str | None = None,
           min_confidence: float | None = None,
           like: str | None = None, device: str | None = None) -> list[dict]:
    """Rank photos by a text query, or by similarity to another photo (``like``).

    With no query at all this degenerates to a pure structured filter, which is
    exactly what you want for "everything of this person in 2009".
    """
    import numpy as np  # noqa: PLC0415

    from . import catalog, embed as E  # noqa: PLC0415

    root = Path(root).resolve()
    conn = catalog.connect(root)
    model = E.model_id()

    allow = _filters(conn, person=person, year=year, place=place, klass=klass,
                     min_confidence=min_confidence)
    if allow is not None and not allow:
        return []

    scores: dict[str, float] = {}
    if query or like:
        # Resolve --like FIRST. Doing it after loading the matrix meant a typo'd
        # path on a library with no embeddings returned "no matches" — an empty
        # result that looks like an answer instead of a mistake.
        q = None
        if like:
            import os  # noqa: PLC0415

            row = conn.execute("SELECT sha256 FROM files WHERE path=?",
                               (os.path.normcase(os.path.abspath(like)),)).fetchone()
            if row is None or not row["sha256"]:
                raise ValueError(f"not in the catalog: {like}")
            r2 = conn.execute("SELECT vec FROM embeddings WHERE sha256=? AND model=?",
                              (row["sha256"], model)).fetchone()
            if r2 is None:
                raise ValueError(
                    f"no embedding for {like} — run `photos index` over its library first")
            q = catalog.unpack_vec(r2["vec"])[None, :]

        keys, mat = catalog.load_matrix(conn, model)
        if not keys:
            raise ValueError(
                f"no embeddings in this catalog for {model}. Run:  "
                f"navig explore photos index {root}")
        if q is None:
            q = E.embed_text([query], device=device)
        sims = (mat @ q[0]).astype("float32")
        order = np.argsort(-sims)
        for i in order:
            k = keys[int(i)]
            if allow is not None and k not in allow:
                continue
            scores[k] = float(sims[int(i)])
            if len(scores) >= limit * 4:
                break
    else:
        for k in list(allow or [])[: limit * 4]:
            scores[k] = 0.0

    if not scores:
        return []

    out: list[dict] = []
    seen_paths: set[str] = set()
    for sha, sc in sorted(scores.items(), key=lambda kv: -kv[1]):
        f = conn.execute(
            "SELECT path, rel, name FROM files WHERE sha256=? AND present=1 LIMIT 1",
            (sha,)).fetchone()
        if f is None or f["path"] in seen_paths:
            continue
        seen_paths.add(f["path"])
        d = conn.execute("SELECT value, source, confidence FROM dates WHERE sha256=?",
                         (sha,)).fetchone()
        c = conn.execute(
            "SELECT class FROM classes WHERE sha256=? ORDER BY score DESC LIMIT 1",
            (sha,)).fetchone()
        names = [r["name"] for r in conn.execute(
            """SELECT DISTINCT p.name FROM faces f JOIN people p ON p.person_id=f.person_id
               WHERE f.sha256=? AND p.name IS NOT NULL""", (sha,))]
        out.append({
            "score": round(sc, 4), "path": f["path"], "rel": f["rel"], "name": f["name"],
            "date": d["value"] if d else None,
            "date_source": d["source"] if d else None,
            "date_confidence": d["confidence"] if d else None,
            "class": c["class"] if c else None,
            "people": names,
        })
        if len(out) >= limit:
            break
    return out
