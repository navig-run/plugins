"""Group every text file of an extracted game by language and build a language index.

``collect(game)`` walks ``data/`` (the raw extracted tree) and ``extract/text`` (engine dumps),
detects the language of each text file from its path, copies it into
``extract/loc/<lang>/<source>/<relative path>`` and writes ``extract/loc/_index.json``.
Copies are byte-for-byte (no transcoding); nothing is ever moved or deleted.

Detection is deliberately conservative — a wrong ``und`` only files a file generically, a wrong
language would corrupt the index, so two-letter codes are accepted only as a directory name,
inside brackets/parens, or as an uppercase delimited tag (``_RUS_``). Full language words
(``russian``, ``english``…) are always accepted.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Callable, Optional

from navig_mobile.unpack.pipeline import TEXT_EXT

Log = Callable[[str], None]

# Full language words → ISO 639-1 (matched as whole tokens, any case, incl. Cyrillic names).
WORDS = {
    "russian": "ru", "русск": "ru", "рус": "ru", "английск": "en", "english": "en",
    "german": "de", "немецк": "de", "french": "fr", "французск": "fr",
    "spanish": "es", "испанск": "es", "italian": "it", "польск": "pl", "polish": "pl",
    "portuguese": "pt", "brazilian": "pt", "chinese": "zh", "китайск": "zh",
    "japanese": "ja", "japan": "ja", "korean": "ko", "turkish": "tr", "dutch": "nl",
    "hungarian": "hu", "czech": "cs", "swedish": "sv", "danish": "da", "norwegian": "no",
    "finnish": "fi", "romanian": "ro", "bulgarian": "bg", "serbian": "sr", "croatian": "hr",
    "ukrainian": "uk", "arabic": "ar", "thai": "th", "vietnamese": "vi", "greek": "el",
}
# Two/three-letter codes accepted in brackets, parens or as an uppercase delimited tag.
CODES = {"ru", "en", "de", "fr", "es", "it", "pl", "pt", "br", "cn", "jp", "kr", "tr",
         "nl", "hu", "cs", "sk", "sv", "da", "no", "fi", "ro", "bg", "sr", "hr", "uk",
         "ar", "th", "vi", "el", "rus", "eng", "ger", "fra", "spa", "ita", "pol", "por",
         "chi", "jpn", "kor"}
_BRACKET = re.compile(r"[(\[][ ._-]*([A-Za-z]{2,3})[ ._-]*[)\]]")
_UPPER_TAG = re.compile(r"(?:^|[^A-Za-z])([A-Z]{2,3})(?=[^A-Za-z]|$)")
# Three-letter tags collapse onto the two-letter bucket the index uses.
NORM = {"rus": "ru", "eng": "en", "ger": "de", "fra": "fr", "spa": "es", "ita": "it",
        "pol": "pl", "por": "pt", "chi": "zh", "jpn": "ja", "kor": "ko", "dut": "nl",
        "hun": "hu", "cze": "cs", "swe": "sv", "dan": "da", "nor": "no", "fin": "fi",
        "rom": "ro", "bul": "bg", "srb": "sr", "hrv": "hr", "ukr": "uk", "ara": "ar",
        "tha": "th", "vie": "vi", "ell": "el", "gre": "el"}


def detect_lang(rel: Path) -> str:
    """Language code for one file path (relative), ``"und"`` when unknown, ``"multi"`` if torn."""
    dirs = [p for p in rel.parts[:-1]]
    stem = rel.stem
    tokens = set(re.split(r"[^0-9a-zа-яё]+", " ".join(dirs).lower())) | \
        set(re.split(r"[^0-9a-zа-яё]+", stem.lower()))

    # 1. A directory that IS the language (locale/, ru/, russian/) — strongest evidence.
    for d in reversed(dirs):
        key = d.lower()
        if key in WORDS:
            return WORDS[key]
        if key in CODES:
            return NORM.get(key, key)

    # 2. Full language words anywhere in the path.
    found = {WORDS[t] for t in tokens if t in WORDS}

    # 3. Bracketed/parens codes and uppercase delimited tags in file/dir names.
    for part in dirs + [stem]:
        for rx in (_BRACKET, _UPPER_TAG):
            for m in rx.finditer(part):
                tag = m.group(1).lower()
                if tag in CODES:
                    found.add("br" if tag == "br" else NORM.get(tag, tag))

    if len(found) == 1:
        return found.pop()
    if len(found) > 1:
        return "multi"
    return "und"


def _copy(src_root: Path, f: Path, out: Path, log_count: dict) -> str:
    lang = detect_lang(f.relative_to(src_root))
    dest = out / lang / src_root.name / f.relative_to(src_root)
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)
        log_count["files"] += 1
        log_count["bytes"] += f.stat().st_size
    return lang


def collect(game: Path, log: Optional[Log] = None) -> dict:
    """Copy every text file into ``extract/loc/<lang>/`` and write ``extract/loc/_index.json``."""
    out = game / "extract" / "loc"
    index: dict[str, dict] = {}
    counts: dict = {"files": 0, "bytes": 0}
    for name in ("data", "extract/text"):
        src = game / name
        if not src.exists():
            continue
        for f in src.rglob("*"):
            if not f.is_file() or f.suffix.lower() not in TEXT_EXT:
                continue
            lang = _copy(src, f, out, counts)
            entry = index.setdefault(lang, {"files": 0, "bytes": 0})
            entry["files"] += 1
            entry["bytes"] += f.stat().st_size
    out.mkdir(parents=True, exist_ok=True)
    (out / "_index.json").write_text(
        json.dumps({"langs": index, "total_files": counts["files"], "total_bytes": counts["bytes"]},
                   indent=2, ensure_ascii=False), encoding="utf-8")
    if log:
        log(f"localization: {counts['files']} files across {len(index)} language bucket(s)")
    return {"lang_files": counts["files"], "langs": sorted(index)}
