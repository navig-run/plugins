"""Read the text inside a file so the cabinet can be searched by what a document *says*.

Thin wrapper over ``navig.inbox.extract`` with two settings that are not negotiable
here, because the files are ID cards and medical records:

* ``ExtractPolicy(mode="local")`` — the default ``auto`` mode may send an image to a
  cloud vision model and audio to a cloud transcriber. Local Tesseract / local Whisper
  only; a missing local engine means no text, never a network call.
* ``cache=None`` — the shared extraction cache is a plaintext store. OCR of a passport
  must never be written anywhere outside the encrypted catalog.

Extraction runs on the original file the operator pointed at, before it is encrypted;
the cabinet never decrypts to a temp file just to read text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

MAX_TEXT_CHARS = 200_000          # a 30-page PDF of dense text fits; a novel does not need to
MAX_PLAIN_TEXT_BYTES = 5_000_000  # the text fast-path in the extractor has no byte cap of its own

_TEXT_KINDS = {"pdf", "image", "office", "text"}
_MEDIA_KINDS = {"audio", "video"}


@dataclass
class TextResult:
    text: str = ""
    source: str = ""               # "pypdf", "tesseract", "whisper_local"… or "" when none
    notes: list[str] = field(default_factory=list)


def _extract_standalone(path: Path, kind: str, res: TextResult, why: Exception) -> TextResult:
    """Without navig: plain text and Word documents are still read, locally.

    PDFs, scans (OCR) and recordings go through navig's extractor; on their own they are
    stored and searchable by title and tags, and the note says so.
    """
    suffix = path.suffix.lower()
    try:
        if kind == "text":
            res.text = path.read_text(encoding="utf-8", errors="replace").strip()[:MAX_TEXT_CHARS]
            res.source = "text"
            return res
        if suffix == ".docx":
            import docx  # python-docx, a navig-cabinet dependency

            doc = docx.Document(str(path))
            res.text = "\n".join(p.text for p in doc.paragraphs if p.text).strip()[:MAX_TEXT_CHARS]
            res.source = "python-docx"
            return res
    except Exception as exc:  # noqa: BLE001 — never raises; the problem becomes a note
        res.notes.append(f"could not read text: {exc}")
        return res
    res.notes.append(
        "reading text from PDFs, scans and recordings needs navig (pip install navig) — "
        f"stored, searchable by title and tags ({why.__class__.__name__})"
    )
    return res


def extract_text(path: Path, kind: str, *, transcribe: bool = False) -> TextResult:
    """Best-effort local text for *path*. Never raises; problems land in ``notes``."""
    res = TextResult()
    if kind in _MEDIA_KINDS and not transcribe:
        return res  # transcription is slow and heavy — only when asked for (--transcribe)
    if kind not in _TEXT_KINDS | _MEDIA_KINDS:
        return res
    try:
        size = path.stat().st_size
    except OSError as exc:
        res.notes.append(f"cannot read: {exc}")
        return res
    if kind == "text" and size > MAX_PLAIN_TEXT_BYTES:
        res.notes.append("text file too large to index — stored, not searchable by content")
        return res

    try:
        from navig.inbox.extract import ExtractPolicy, extract
    except Exception as exc:  # noqa: BLE001 — standalone install without navig core
        return _extract_standalone(path, kind, res, exc)

    policy = ExtractPolicy(mode="local")
    if size > policy.max_bytes:
        res.notes.append(
            f"too large to read text from ({size // 1_000_000} MB > "
            f"{policy.max_bytes // 1_000_000} MB) — stored, searchable by title and tags only"
        )
        return res

    out = extract(path, policy=policy, cache=None)
    res.text = (out.text or "").strip()[:MAX_TEXT_CHARS]
    engines = [e for e in out.extracted_by if e not in {"pillow", "mutagen", "passthrough"}]
    res.source = "+".join(engines)
    if not res.text:
        for err in out.errors:
            res.notes.append(err)
        if kind in {"image", "pdf"}:
            # A photo with no words in it is normal; a scan that reads as empty because
            # the OCR engine is missing is not — only the second deserves a note.
            try:
                from navig.core.ocr import ocr_unavailable_reason

                reason = ocr_unavailable_reason()
            except Exception:  # noqa: BLE001
                reason = None
            if reason:
                res.notes.append(f"OCR unavailable: {reason}")
    return res
