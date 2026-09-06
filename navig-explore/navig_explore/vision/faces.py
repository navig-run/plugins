"""Face detection + embedding, behind an engine flag.

Default is **YuNet + SFace** (OpenCV Zoo, Apache-2.0). They ship inside the
already-installed OpenCV, cost no extra Python dependency, and carry no licence
restriction. ``arcface`` (InsightFace ``buffalo_l``) is opt-in and measurably
better on low-resolution and profile faces — but its *model weights* are licensed
for non-commercial research only, unchanged as of 2026, so it must never become
the silent default.

Orientation matters more than usual here. This library's recovered photos lost
their EXIF Orientation tag along with the rest of their metadata, and a detector
finds nothing in a face lying on its side. When the upright pass comes up empty
we retry the rotations rather than record "no faces" for a portrait full of them.
"""
from __future__ import annotations

import os
import threading
import urllib.request
from pathlib import Path

ENGINES = ("yunet-sface", "arcface")
DEFAULT_ENGINE = "yunet-sface"

# OpenCV Zoo, pinned by filename so a silent upstream model swap can't move results.
_ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models"
_MODELS = {
    "detect": (f"{_ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
               "face_detection_yunet_2023mar.onnx"),
    "recognize": (f"{_ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
                  "face_recognition_sface_2021dec.onnx"),
}

_lock = threading.Lock()
_engine_cache: dict[str, object] = {}


def cache_dir() -> Path:
    d = Path(os.environ.get("NAVIG_HOME", Path.home() / ".navig")) / "cache" / "models" / "opencv-zoo"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_models(*, quiet: bool = False) -> dict[str, Path]:
    """Fetch YuNet/SFace once into the navig model cache."""
    out = {}
    for kind, (url, fname) in _MODELS.items():
        dest = cache_dir() / fname
        if not dest.exists() or dest.stat().st_size == 0:
            if not quiet:
                print(f"[faces] fetching {fname} …", flush=True)
            tmp = dest.with_suffix(".part")
            urllib.request.urlretrieve(url, tmp)  # noqa: S310 - pinned github URL
            tmp.replace(dest)
        out[kind] = dest
    return out


# YuNet degrades badly when handed a native-resolution photo: on a 2592x1944 frame
# it boxed a hand and an ear at 0.6 confidence and found no real face at all.
# Downscaling the longest side to this before detection removed every false positive
# in testing *and* made detection ~4x cheaper. Measured, not guessed.
DETECT_MAXDIM = 1024

# Kept deliberately permissive. A genuine face in this library's low-quality
# recovery salvage scores 0.63, so the commonly-cited 0.85 — and even 0.65 —
# discards it. The resize above, not the threshold, is what suppresses noise:
# at 0.6 *with* the resize, the false-positive hand and ear both vanish.
DETECT_THRESHOLD = 0.6

# Only skip the rotation search when upright is *confidently* right. Accepting any
# hit at 0 degrees looks like a harmless fast path but isn't: a 0.60 false positive
# on a sideways photo suppressed the search that would have found the real 0.63 face
# at 90 degrees. Ordinary upright photos clear this bar and still cost one pass.
CONFIDENT_UPRIGHT = 0.80


class YuNetSFace:
    """OpenCV Zoo detector + recogniser. 128-d embeddings, cosine-comparable."""

    dim = 128
    name = "yunet-sface"

    def __init__(self, score_threshold: float = DETECT_THRESHOLD,
                 nms: float = 0.3, top_k: int = 128):
        import cv2  # noqa: PLC0415

        paths = ensure_models(quiet=True)
        self._cv2 = cv2
        self._det = cv2.FaceDetectorYN.create(
            str(paths["detect"]), "", (320, 320), score_threshold, nms, top_k)
        self._rec = cv2.FaceRecognizerSF.create(str(paths["recognize"]), "")
        self._lock = threading.Lock()

    @staticmethod
    def _fit(bgr):
        """Downscale for detection; returns ``(image, scale_applied)``."""
        import cv2  # noqa: PLC0415

        longest = max(bgr.shape[:2])
        if longest <= DETECT_MAXDIM:
            return bgr, 1.0
        s = DETECT_MAXDIM / longest
        return cv2.resize(bgr, None, fx=s, fy=s, interpolation=cv2.INTER_AREA), s

    def _detect_once(self, bgr):
        h, w = bgr.shape[:2]
        # YuNet needs its input size set to the frame it is about to see.
        self._det.setInputSize((w, h))
        ok, faces = self._det.detect(bgr)
        if not ok or faces is None:
            return []
        return list(faces)

    def detect(self, bgr, *, try_rotations: bool = True):
        """Return ``(rotation, faces, work_image)`` for the most upright orientation.

        Rotation is chosen by the **best single** detection, not the sum: summing
        rewards whichever angle hallucinates the most boxes, which is precisely how
        an early version settled on 180° for a photo that needed 90°.

        ``work_image`` is the downscaled, rotated frame the boxes belong to — the
        caller must pass it back to :meth:`embed` so alignment lands on the face.
        """
        cv2 = self._cv2
        with self._lock:
            small, _ = self._fit(bgr)
            faces = self._detect_once(small)
            upright = max((float(f[-1]) for f in faces), default=0.0)
            if not try_rotations or upright >= CONFIDENT_UPRIGHT:
                return 0, faces, small
            best = (0, faces, small, upright)
            for rot, code in ((90, cv2.ROTATE_90_CLOCKWISE),
                              (270, cv2.ROTATE_90_COUNTERCLOCKWISE),
                              (180, cv2.ROTATE_180)):
                work = cv2.rotate(small, code)
                cand = self._detect_once(work)
                score = max((float(f[-1]) for f in cand), default=0.0)
                if score > best[3]:
                    best = (rot, cand, work, score)
            return best[0], best[1], best[2]

    def embed(self, work_bgr, face_row):
        with self._lock:
            aligned = self._rec.alignCrop(work_bgr, face_row)
            feat = self._rec.feature(aligned)
        return feat.ravel()


class ArcFace:
    """InsightFace ``buffalo_l`` — better on hard faces, non-commercial weights."""

    dim = 512
    name = "arcface"

    def __init__(self, det_size: int = 640):
        try:
            from insightface.app import FaceAnalysis  # noqa: PLC0415
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "--faces-engine arcface needs InsightFace:\n"
                "  py -3.13 -m pip install insightface onnxruntime-gpu\n"
                "NOTE: the buffalo_l model weights are licensed for NON-COMMERCIAL "
                "research use only. Use the default yunet-sface engine for anything "
                "that might ship."
            ) from exc
        self._app = FaceAnalysis(name="buffalo_l")
        self._app.prepare(ctx_id=0, det_size=(det_size, det_size))
        self._lock = threading.Lock()

    def detect(self, bgr, *, try_rotations: bool = True):
        import cv2  # noqa: PLC0415

        with self._lock:
            faces = self._app.get(bgr)
            if faces or not try_rotations:
                return 0, faces, bgr
            best = (0, [], bgr, 0.0)
            for rot, code in ((90, cv2.ROTATE_90_CLOCKWISE),
                              (270, cv2.ROTATE_90_COUNTERCLOCKWISE),
                              (180, cv2.ROTATE_180)):
                work = cv2.rotate(bgr, code)
                cand = self._app.get(work)
                score = max((float(f.det_score) for f in cand), default=0.0)
                if score > best[3]:
                    best = (rot, cand, work, score)
            return best[0], best[1], best[2]

    def embed(self, work_bgr, face):  # noqa: ARG002 - computed during .get()
        return face.normed_embedding.ravel()


def get_engine(name: str = DEFAULT_ENGINE):
    """Build (once per process) the requested face engine."""
    if name not in ENGINES:
        raise ValueError(f"unknown faces engine {name!r}; pick one of {', '.join(ENGINES)}")
    with _lock:
        if name not in _engine_cache:
            _engine_cache[name] = ArcFace() if name == "arcface" else YuNetSFace()
        return _engine_cache[name]


def normalise_rows(engine, faces, work_bgr) -> list[dict]:
    """Flatten either engine's output, with boxes as 0-1 fractions.

    Fractions rather than pixels because detection runs on a downscaled frame:
    storing raw pixel boxes would silently bind every face row to whatever
    ``DETECT_MAXDIM`` happened to be on the day it was indexed.
    """
    h, w = work_bgr.shape[:2]
    out = []
    if engine.name == "arcface":
        for f in faces:
            x1, y1, x2, y2 = [float(v) for v in f.bbox]
            out.append({"x": x1 / w, "y": y1 / h, "w": (x2 - x1) / w, "h": (y2 - y1) / h,
                        "score": float(f.det_score), "raw": f})
    else:
        for f in faces:
            out.append({"x": float(f[0]) / w, "y": float(f[1]) / h,
                        "w": float(f[2]) / w, "h": float(f[3]) / h,
                        "score": float(f[-1]), "raw": f})
    return out
