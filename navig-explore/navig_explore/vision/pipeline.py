"""The decode passes: hash, measure, embed, classify — then faces.

Two passes, not one, and the reason is measured rather than aesthetic. Decoding a
photo costs ~20 ms; detecting faces in it costs 100-350 ms because a library whose
EXIF orientation was destroyed forces a rotation search. Running face detection
over all ~97k files to serve the ~30% that are actually photographs would dominate
the entire job. So:

* :func:`run_index` decodes once and produces sha256, dimensions, pHash, a blur
  score, a SigLIP embedding and a content class. ~600 img/s.
* :func:`run_faces` decodes again, but only for files the classifier called
  ``photo`` or ``webcam``. Re-decoding 30% is far cheaper than detecting on 100%.

Both are resumable: work is claimed from the catalog, and results are committed in
batches, so an interrupted library run loses only the batch in flight. That exact
failure — losing a finished pass to an interrupted run — is what bit the earlier
dedup work on this library.
"""
from __future__ import annotations

import hashlib
import queue
import threading
import time
from pathlib import Path

BATCH = 256          # GPU batch; 256 measured at ~604 img/s on an RTX 3070 Ti
QUEUE_DEPTH = 4      # batches of slack so decode and GPU overlap
MIN_FACE_DIM = 256   # below this an image cannot hold a usable face


class _Rec:
    __slots__ = ("path", "sha256", "size", "w", "h", "ok", "reason", "missing",
                 "phash", "quality", "tensor")

    def __init__(self, path):
        self.path = path
        self.sha256 = None
        self.size = 0
        self.w = self.h = 0
        self.ok = False
        self.reason = ""
        # "gone" and "corrupt" are different answers and must never be summed.
        # A stale meta.jsonl (the library moved since `probe` last ran) otherwise
        # reports tens of thousands of perfectly healthy photos as undecodable.
        self.missing = False
        self.phash = None
        self.quality = None
        self.tensor = None


def _sha256_and_bytes(path: str) -> tuple[str, bytes]:
    """Hash and return the bytes in one read — the file is opened exactly once."""
    with open(path, "rb") as fh:
        data = fh.read()
    return hashlib.sha256(data).hexdigest(), data


def _decode_one(path: str, preprocess) -> _Rec:
    import io  # noqa: PLC0415

    import numpy as np  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    rec = _Rec(path)
    try:
        rec.sha256, data = _sha256_and_bytes(path)
        rec.size = len(data)
    except FileNotFoundError:
        rec.missing = True
        rec.reason = "missing: no longer at this path"
        return rec
    except OSError as exc:
        rec.reason = f"read: {type(exc).__name__}"
        return rec

    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        im = im.convert("RGB")
    except Exception as exc:  # noqa: BLE001 - undecodable bytes are DATA here, not an error
        # 5,664 files in this library are bad recovery reconstructions. They must be
        # recorded as a known class, never swallowed into a silent skip counter.
        rec.reason = f"decode: {type(exc).__name__}"
        return rec

    rec.ok = True
    rec.w, rec.h = im.size

    try:
        import imagehash  # noqa: PLC0415
        rec.phash = str(imagehash.phash(im))
    except Exception:  # noqa: BLE001
        rec.phash = None

    try:
        import cv2  # noqa: PLC0415
        small = im if max(im.size) <= 512 else im.resize(
            (max(1, im.width * 512 // max(im.size)), max(1, im.height * 512 // max(im.size))))
        g = np.asarray(small.convert("L"))
        rec.quality = float(cv2.Laplacian(g, cv2.CV_64F).var())
    except Exception:  # noqa: BLE001
        rec.quality = None

    if preprocess is not None:
        try:
            rec.tensor = preprocess(im)
        except Exception:  # noqa: BLE001
            rec.tensor = None
    return rec


def run_index(root: Path, *, workers: int = 12, limit: int | None = None,
              do_embed: bool = True, device: str | None = None,
              batch: int = BATCH, quiet: bool = False,
              progress_every: float = 5.0) -> dict:
    """Decode pass: sha256 + dimensions + pHash + blur + embedding + class."""
    from . import catalog, classify, embed as E  # noqa: PLC0415

    root = Path(root).resolve()
    conn = catalog.connect(root)
    catalog.ingest_meta_jsonl(conn, root, quiet=quiet)

    preprocess = None
    model_id = None
    if do_embed:
        _, preprocess, dev = E.load(device=device)
        model_id = E.model_id()
        if not quiet:
            print(f"[index] {model_id} on {dev}", flush=True)

    pending = [dict(r) for r in catalog.iter_pending(conn, root, limit=limit)]
    total = len(pending)
    if not quiet:
        print(f"[index] {total} files to do", flush=True)
    if not total:
        return {"done": 0, "total": 0, "undecodable": 0}

    q: queue.Queue = queue.Queue(maxsize=batch * QUEUE_DEPTH)
    src = iter(pending)
    src_lock = threading.Lock()

    def worker():
        while True:
            with src_lock:
                item = next(src, None)
            if item is None:
                break
            q.put(_decode_one(item["path"], preprocess))
        q.put(None)

    pool = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, workers))]
    for t in pool:
        t.start()

    stats = {"done": 0, "total": total, "undecodable": 0, "missing": 0, "embedded": 0}
    finished = 0
    buf: list[_Rec] = []
    t0 = last = time.time()

    def flush(records: list[_Rec]) -> None:
        if not records:
            return
        vecs = None
        with_tensor = [r for r in records if r.tensor is not None]
        if do_embed and with_tensor:
            import torch  # noqa: PLC0415

            net, _, dev = E.load(device=device)
            stack = torch.stack([r.tensor for r in with_tensor]).to(dev)
            if dev.startswith("cuda"):
                stack = stack.half()
            with torch.no_grad():
                f = net.encode_image(stack)
                f = f / f.norm(dim=-1, keepdim=True)
            vecs = f.float().cpu().numpy()

        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        # Files that have moved away are flagged, not retried. Without this they
        # stay pending forever and every later run re-walks them.
        gone = [(r.path,) for r in records if r.missing]
        if gone:
            conn.executemany("UPDATE files SET present=0 WHERE path=?", gone)
        conn.executemany("UPDATE files SET sha256=? WHERE path=?",
                         [(r.sha256, r.path) for r in records if r.sha256])
        conn.executemany(
            """INSERT INTO assets (sha256, bytes, w, h, decoded, fail_reason,
                                   quality, phash, indexed_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(sha256) DO UPDATE SET
                 bytes=excluded.bytes, w=excluded.w, h=excluded.h,
                 decoded=excluded.decoded, fail_reason=excluded.fail_reason,
                 quality=excluded.quality, phash=excluded.phash,
                 indexed_at=excluded.indexed_at""",
            [(r.sha256, r.size, r.w, r.h, 1 if r.ok else 0, r.reason,
              r.quality, r.phash, now) for r in records if r.sha256],
        )
        if vecs is not None and len(with_tensor):
            conn.executemany(
                "INSERT OR REPLACE INTO embeddings (sha256, model, dim, vec) VALUES (?,?,?,?)",
                [(r.sha256, model_id, int(vecs.shape[1]), catalog.pack_vec(vecs[i]))
                 for i, r in enumerate(with_tensor)],
            )
            stats["embedded"] += len(with_tensor)
            try:
                labelled = classify.best(vecs)
                conn.executemany(
                    "INSERT OR REPLACE INTO classes (sha256, class, score) VALUES (?,?,?)",
                    [(r.sha256, labelled[i][0], labelled[i][1])
                     for i, r in enumerate(with_tensor)],
                )
            except Exception as exc:  # noqa: BLE001 - classification is a bonus, not the job
                if not quiet:
                    print(f"[index] classify skipped: {exc}", flush=True)
        conn.commit()

    while finished < len(pool):
        rec = q.get()
        if rec is None:
            finished += 1
            continue
        buf.append(rec)
        stats["done"] += 1
        if rec.missing:
            stats["missing"] += 1
        elif not rec.ok:
            stats["undecodable"] += 1
        if len(buf) >= batch:
            flush(buf)
            buf = []
        if not quiet and time.time() - last >= progress_every:
            el = time.time() - t0
            rate = stats["done"] / el if el else 0
            eta = (total - stats["done"]) / rate if rate else 0
            print(f"[index] {stats['done']}/{total}  {rate:.0f}/s  eta {eta/60:.1f}m",
                  flush=True)
            last = time.time()
    flush(buf)

    for t in pool:
        t.join(timeout=1)
    el = time.time() - t0
    if not quiet:
        indexed = stats["done"] - stats["missing"] - stats["undecodable"]
        print(f"[index] done in {el/60:.1f}m ({stats['done']/el if el else 0:.0f}/s): "
              f"{indexed} indexed · {stats['undecodable']} undecodable · "
              f"{stats['missing']} missing", flush=True)
        if stats["missing"]:
            print(f"[index] {stats['missing']} files in meta.jsonl are no longer at "
                  f"their recorded path — the sidecar predates a reorganisation. "
                  f"Refresh it with:  navig explore probe {root}", flush=True)
    return stats


def run_faces(root: Path, *, engine: str = "yunet-sface", workers: int = 8,
              limit: int | None = None, only_classes: tuple[str, ...] = ("photo", "webcam"),
              min_dim: int = MIN_FACE_DIM, quiet: bool = False,
              progress_every: float = 5.0) -> dict:
    """Face pass — only over files the classifier considers photographs."""
    import numpy as np  # noqa: PLC0415

    from . import catalog, faces as FA  # noqa: PLC0415

    root = Path(root).resolve()
    conn = catalog.connect(root)
    eng = FA.get_engine(engine)

    placeholders = ",".join("?" * len(only_classes))
    rows = conn.execute(
        f"""
        SELECT DISTINCT f.path, a.sha256
        FROM files f
        JOIN assets a  ON a.sha256 = f.sha256
        JOIN classes c ON c.sha256 = a.sha256
        WHERE f.present=1 AND f.root=? AND a.decoded=1
          AND c.class IN ({placeholders})
          AND a.w >= ? AND a.h >= ?
          AND NOT EXISTS (SELECT 1 FROM faces x WHERE x.sha256 = a.sha256 AND x.engine = ?)
        ORDER BY a.sha256
        {"LIMIT " + str(int(limit)) if limit else ""}
        """,
        (str(root), *only_classes, min_dim, min_dim, eng.name),
    ).fetchall()

    total = len(rows)
    if not quiet:
        print(f"[faces] engine={eng.name} · {total} images to scan", flush=True)
    if not total:
        return {"images": 0, "faces": 0, "rotated": 0}

    out_q: queue.Queue = queue.Queue(maxsize=256)
    src = iter([dict(r) for r in rows])
    lock = threading.Lock()

    def worker():
        import cv2  # noqa: PLC0415
        while True:
            with lock:
                item = next(src, None)
            if item is None:
                break
            try:
                buf = np.fromfile(item["path"], dtype=np.uint8)
                bgr = cv2.imdecode(buf, cv2.IMREAD_COLOR)
                if bgr is None:
                    out_q.put((item["sha256"], 0, []))
                    continue
                rot, det, work = eng.detect(bgr)
                found = []
                for r in FA.normalise_rows(eng, det, work):
                    try:
                        vec = eng.embed(work, r["raw"])
                    except Exception:  # noqa: BLE001
                        continue
                    found.append((r, vec))
                out_q.put((item["sha256"], rot, found))
            except Exception:  # noqa: BLE001
                out_q.put((item["sha256"], 0, []))
        out_q.put(None)

    pool = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, workers))]
    for t in pool:
        t.start()

    stats = {"images": 0, "faces": 0, "rotated": 0}
    finished = 0
    pending_rows: list[tuple] = []
    pending_rot: list[tuple] = []
    t0 = last = time.time()

    while finished < len(pool):
        item = out_q.get()
        if item is None:
            finished += 1
            continue
        sha, rot, found = item
        stats["images"] += 1
        if rot:
            stats["rotated"] += 1
        pending_rot.append((rot, sha))
        for r, vec in found:
            pending_rows.append((sha, r["x"], r["y"], r["w"], r["h"], r["score"],
                                 rot, eng.name, int(len(vec)), catalog.pack_vec(vec)))
        stats["faces"] += len(found)

        if len(pending_rot) >= 200:
            conn.executemany("UPDATE assets SET upright=? WHERE sha256=?", pending_rot)
            conn.executemany(
                """INSERT INTO faces (sha256,x,y,w,h,score,rotation,engine,dim,vec)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""", pending_rows)
            conn.commit()
            pending_rot, pending_rows = [], []

        if not quiet and time.time() - last >= progress_every:
            el = time.time() - t0
            rate = stats["images"] / el if el else 0
            print(f"[faces] {stats['images']}/{total}  {rate:.1f}/s  "
                  f"faces={stats['faces']}  eta {(total-stats['images'])/rate/60 if rate else 0:.1f}m",
                  flush=True)
            last = time.time()

    if pending_rot:
        conn.executemany("UPDATE assets SET upright=? WHERE sha256=?", pending_rot)
        conn.executemany(
            """INSERT INTO faces (sha256,x,y,w,h,score,rotation,engine,dim,vec)
               VALUES (?,?,?,?,?,?,?,?,?,?)""", pending_rows)
    conn.commit()
    for t in pool:
        t.join(timeout=1)
    if not quiet:
        el = time.time() - t0
        print(f"[faces] done {stats['images']} images / {stats['faces']} faces "
              f"in {el/60:.1f}m · {stats['rotated']} needed rotating", flush=True)
    return stats
