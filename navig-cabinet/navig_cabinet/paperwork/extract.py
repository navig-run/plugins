"""Text extraction, wrapping ``navig.inbox.extract`` with a privacy-safe default.

The one substantive decision here is the policy. ``ExtractPolicy.from_config()``
defaults to ``mode="auto"``, and ``allow_cloud`` is ``mode != "local"`` — so the
inherited default would ship a person's tax notices, bank statements and health
paperwork to a paid cloud vision provider. This module **forces ``local``** and makes
cloud an explicit, per-invocation opt-in.

Also here: our own SHA-256. ``ExtractResult.content_hash`` is empty for text files
(the fast path returns before hashing) and for anything over the size cap, so it
cannot be the dedupe key.
"""

from __future__ import annotations

import hashlib
import logging
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

# Suffixes worth opening. Everything else is classified on its filename alone.
#
# Images are readable: a photographed letter is the normal shape of paper mail once a
# phone is the scanner, and ``navig.inbox.extract`` already OCRs them locally. Without
# this entry a `.jpg` of a CAF letter was classified on its filename alone — even
# with `--all-types` — and looked like an unreadable file rather than a letter.
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".heic", ".webp", ".tif", ".tiff", ".bmp"})
READABLE = frozenset(
    {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".txt", ".md", ".csv",
     ".html", ".htm", ".rtf", ".odt", ".json", ".xml"}
) | IMAGE_SUFFIXES


@dataclass
class Extracted:
    text: str = ""
    method: str = "none"          # pypdf | ocr | docx | passthrough | none | error
    pages: int = 0
    errors: tuple[str, ...] = ()


def ocr_unavailable_reason() -> str | None:
    """Why images cannot be read right now, or None when local OCR works."""
    try:
        from navig.core.ocr import ocr_unavailable_reason as _reason

        return _reason()
    except Exception as exc:  # noqa: BLE001 — the probe itself failing is the reason
        return f"navig.core.ocr unavailable: {exc}"


def missing_extractors() -> list[str]:
    """Which readers are absent. Empty means PDFs and Office files can be read.

    This exists because the failure is otherwise invisible and dangerous. With
    ``pypdf`` missing, ``navig.inbox.extract`` returns an empty string and puts the
    reason in ``errors`` — so every document silently degrades to filename-only
    classification, the personal-document veto never sees any content, and a medical
    bill named ``facture-….pdf`` files itself under ``finance/invoices/``. A scan that
    cannot read its documents must say so rather than produce a confident-looking plan.
    """
    missing: list[str] = []
    for module, label in (
        ("pypdf", "pypdf (PDF text)"),
        ("fitz", "PyMuPDF (scanned-PDF rendering for OCR)"),
        ("docx", "python-docx (.docx)"),
    ):
        try:
            __import__(module)
        except ImportError:
            missing.append(label)
    return missing


@contextmanager
def _quiet_pdf_readers():
    """Silence pypdf's per-page commentary for the duration of one extraction.

    Real archives are full of PDFs written by decades of different tools, and pypdf
    narrates each quirk — "Ignoring wrong pointing object", a paragraph-long font
    dictionary per embedded Type1 face. Across hundreds of documents that buries the
    scan's own progress output. Genuine failures are not lost: they arrive through
    ``ExtractResult.errors``, which this module surfaces on ``Extracted.errors``.
    """
    noisy = [logging.getLogger(name) for name in ("pypdf", "pypdf.generic", "fontTools", "fitz")]
    previous = [(log, log.level, log.propagate) for log in noisy]
    for log in noisy:
        log.setLevel(logging.ERROR)
        log.propagate = False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            yield
    finally:
        for log, level, propagate in previous:
            log.setLevel(level)
            log.propagate = propagate


def sha256_file(path: str | Path, *, chunk: int = 1 << 20) -> str:
    """Streaming SHA-256. Returns "" when the file cannot be read."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(chunk), b""):
                h.update(block)
    except OSError:
        return ""
    return h.hexdigest()


def extract_text(
    path: str | Path,
    *,
    ocr: bool = True,
    cloud: bool = False,
    max_pages: int = 3,
) -> Extracted:
    """Extract text from *path*. Never raises — a failure is an empty result.

    Only the first *max_pages* pages are read: every signal this plugin looks for
    (SIREN, invoice id, TVA mention, counterparty block) lives on page one, and
    capping the read is what keeps a 12 MB scanned deck from costing a minute.
    """
    p = Path(path)
    if p.suffix.lower() not in READABLE:
        return Extracted(method="none")

    try:
        from navig.inbox.extract import ExtractPolicy, extract as _extract
    except ImportError as exc:
        return Extracted(method="error", errors=(f"navig.inbox.extract unavailable: {exc}",))

    # "local" keeps every byte on this machine; "cloud" is only ever reached by an
    # explicit --cloud-ocr on the command line.
    policy = ExtractPolicy(mode="cloud" if cloud else "local", max_pdf_pages=max_pages)

    try:
        with _quiet_pdf_readers():
            res = _extract(p, policy=policy)
    except Exception as exc:  # noqa: BLE001 — extract() promises not to raise; belt and braces
        return Extracted(method="error", errors=(str(exc),))

    by = list(getattr(res, "extracted_by", ()) or ())
    method = by[-1] if by else ("none" if not res.text else "unknown")
    if not ocr and method == "ocr":
        # Caller asked for no OCR; treat a scanned page as unreadable rather than
        # silently returning text produced by a path they opted out of.
        return Extracted(method="skipped-ocr", errors=("OCR disabled",))

    meta = getattr(res, "metadata", {}) or {}
    return Extracted(
        text=res.text or "",
        method=method,
        pages=int(meta.get("pages") or meta.get("page_count") or 0),
        errors=tuple(getattr(res, "errors", ()) or ()),
    )
