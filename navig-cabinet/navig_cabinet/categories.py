"""What an item is (its *kind*, from the file) and what it is about (its *category*).

The kind is mechanical — mime type and suffix. The category is a suggestion from the
filename and the first page of text, always overridable with ``--category``; guessing
wrong costs one ``navig cabinet edit``, so the rules stay short and readable.
"""

from __future__ import annotations

import mimetypes
import re
import unicodedata
from pathlib import Path

CATEGORIES = (
    "identity", "medical", "insurance", "finance", "housing", "legal",
    "education", "vehicle", "photos", "recordings", "other",
)

_OFFICE = {".doc", ".docx", ".odt", ".rtf", ".xls", ".xlsx", ".ods", ".csv",
           ".ppt", ".pptx", ".odp", ".pages", ".numbers", ".key"}
_ARCHIVE = {".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz"}


def kind_of(name: str) -> tuple[str, str]:
    """``(kind, mime)`` for a filename. Kind is pdf|image|audio|video|office|text|archive|other."""
    suffix = Path(name).suffix.lower()
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if suffix == ".pdf" or mime == "application/pdf":
        return "pdf", "application/pdf"
    if suffix in {".heic", ".heif"}:
        return "image", "image/heic"
    if mime.startswith("image/"):
        return "image", mime
    if mime.startswith("audio/") or suffix in {".m4a", ".opus", ".oga", ".amr"}:
        return "audio", mime
    if mime.startswith("video/") or suffix in {".mkv", ".m4v", ".3gp"}:
        return "video", mime
    if suffix in _OFFICE:
        return "office", mime
    if suffix in _ARCHIVE:
        return "archive", mime
    if mime.startswith("text/") or suffix in {".md", ".txt", ".json", ".xml"}:
        return "text", mime
    return "other", mime


# First match wins, so the most specific categories come first. Patterns run against
# an accent-folded, lowercased haystack (filename + the first part of the text).
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("identity", re.compile(
        r"passport|passeport|id.?card|identity card|carte.?(nationale|d.?identite)|\bcni\b|"
        r"residence permit|titre de sejour|\bvisa\b|birth certificate|acte de naissance|"
        r"driving licen[cs]e|driver.?s licen[cs]e|permis de conduire|livret de famille")),
    ("medical", re.compile(
        r"medical|medecin|doctor|ordonnance|prescription|carte vitale|blood test|"
        r"analyse(s)? (de sang|biolog)|laborato|radiolog|\bmri\b|\birm\b|scanner|vaccin|"
        r"hospital|hopital|clinic|clinique|dentist|dentaire|compte.?rendu|diagnos|"
        # lab results name the analyte and a unit long before they say "medical"
        r"ha?emoglobin|cholesterol|glyc[ae]mi|glucose|creatinin|leucocyt|\bg/dl\b|\bmg/dl\b|mmol/l")),
    ("insurance", re.compile(r"insurance|assurance|mutuelle|policy number|numero de police|attestation d.assurance")),
    ("vehicle", re.compile(r"carte grise|certificat d.immatriculation|vehicle registration|controle technique")),
    ("housing", re.compile(r"\bbail\b|lease|tenancy|\brent\b|loyer|quittance|mortgage|pret immobilier|acte de vente|etat des lieux")),
    ("finance", re.compile(
        r"bank|banque|\brib\b|\biban\b|tax return|avis d.imposition|impot|payslip|bulletin de (paie|salaire)|"
        r"pension|retraite|invoice|facture|receipt|recu")),
    ("legal", re.compile(r"contract|contrat|testament|\bwill\b|notaire|notary|court|tribunal|jugement|power of attorney|procuration")),
    ("education", re.compile(r"diplom|degree|transcript|releve de notes|certificate of|baccalaureat|school|universit")),
)


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def suggest_category(name: str, kind: str, text: str = "") -> str:
    hay = _fold(Path(name).stem.replace("_", " ").replace("-", " ") + " " + text[:4000])
    for category, pattern in _RULES:
        if pattern.search(hay):
            return category
    if kind in {"audio", "video"}:
        return "recordings"
    if kind == "image" and not text.strip():
        return "photos"
    return "other"


def normalise_category(value: str) -> str:
    v = value.strip().lower()
    aliases = {"id": "identity", "health": "medical", "med": "medical", "money": "finance",
               "home": "housing", "car": "vehicle", "school": "education", "photo": "photos",
               "recording": "recordings", "audio": "recordings", "video": "recordings"}
    v = aliases.get(v, v)
    if v not in CATEGORIES:
        raise ValueError(f"unknown category {value!r} — one of: {', '.join(CATEGORIES)}")
    return v


def normalise_tags(tags: list[str] | None) -> list[str]:
    out: list[str] = []
    for raw in tags or []:
        for t in raw.split(","):
            t = t.strip().lower().lstrip("#")
            if t and t not in out:
                out.append(t)
    return out
