"""Rename a whole tree to clean English/Latin names — reviewable, verifiable, reversible.

The job: a folder that grew out of Notion exports and three languages — Cyrillic folder
names, titles truncated at 50 characters, ``(2)`` copies, emoji, ``#hashtags``, Notion
hashes — becomes one where every folder is an English word and every file a readable slug,
text files tagged with their language (and year, when the file says so):
``lyrics/Тексты/Выбежал из леса.md`` → ``lyrics/texts/ru--vybezhal-iz-lesa.md``.

It is four steps, and the first one changes nothing:

* **plan** — walk the tree, compute every new name from a map (folder names, spine-doc names,
  word swaps, which globs get tagged) and write ``rename-plan.csv`` to read before anything moves.
* **apply** — move file by file (MD5 before and after), then rewrite every *reference* to a
  moved path inside the tree's text files: root-relative paths in either separator,
  absolute and JSON-escaped absolute paths, and markdown links / front-matter paths, which are
  resolved from where the referencing file *was* and re-relativised from where it *is*. Every
  text file is backed up before its first rewrite.
* **verify** — every moved file is at its destination with the same MD5, no source is left,
  no path in scope still carries a non-Latin letter, no text file still names an old path.
* **undo** — restores the backed-up text files and moves every file back.

Junctions and symlinks are never followed (a junction inside a space can point outside it).
"""

from __future__ import annotations

import csv
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import quote, unquote

TOOL_DIR = ".mediaexplorer"

TEXT_EXT = {".md", ".markdown", ".txt", ".yaml", ".yml", ".json", ".jsonl", ".csv", ".tsv",
            ".py", ".ps1", ".srt", ".html", ".toml", ".ini"}

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "і": "i", "ї": "yi", "є": "ye", "ґ": "g",
}
_NOTION_HASH = re.compile(r"\s+[0-9a-f]{32}$", re.I)
_DUP_SUFFIX = re.compile(r"\s*\((\d+)\)$")
_YEAR_LINE = re.compile(
    r"^(?:created|date|year|дата|год|создано|updated)\s*[:：]\s*(.+)$", re.I | re.M)
_YEAR = re.compile(r"\b(19[89]\d|20[0-3]\d)\b")
_LANG_LINE = re.compile(r"^language\s*:\s*(\w+)", re.I | re.M)
_LANG_NAMES = {"russian": "ru", "french": "fr", "english": "en", "ukrainian": "uk",
               "русский": "ru", "французский": "fr", "английский": "en"}
_FR = {"je", "tu", "le", "la", "les", "des", "est", "et", "pas", "que", "qui", "c'est", "mon",
       "ma", "moi", "une", "dans", "pour", "avec", "mais", "suis", "tout", "sur", "de", "du", "au",
       "aux", "ton", "ta", "tes", "toi", "ne", "se", "sa", "son", "ses", "par", "plus", "comme",
       "bonjour", "petite", "esprit", "avenir", "meme", "même", "beaucoup", "vérité", "verite",
       "garcon", "garçon", "mauvais", "interne", "religion", "virtuel", "un", "en", "il", "elle",
       "nous", "vous", "ils", "rien", "quoi", "très", "tres", "ça", "ca"}
# Elision is French and nothing else: l', d', j', qu', n', s', c', t', m'.
_FR_ELISION = re.compile(r"\b(?:l|d|j|qu|n|s|c|t|m)['’][a-zà-ÿ]", re.I)
_EN = {"the", "and", "you", "i", "is", "it", "my", "me", "to", "of", "in", "that", "your",
       "on", "we", "with", "don't", "i'm", "this", "are"}


class RenameError(RuntimeError):
    """A plan or a manifest the rename cannot use."""


# ── names ──────────────────────────────────────────────────────────────────────

def has_non_latin(text: str) -> bool:
    return any(ord(c) > 127 for c in text)


def transliterate(text: str) -> str:
    out = []
    for ch in text:
        low = ch.lower()
        if low in _TRANSLIT:
            t = _TRANSLIT[low]
            out.append(t.capitalize() if ch != low and t else t)
        else:
            out.append(ch)
    return "".join(out)


def clean_title(stem: str) -> str:
    """Strip what Notion and copy-paste leave on a name: hash, ``(2)``, ``Copy of``, ``{{``, emoji."""
    s = _NOTION_HASH.sub("", stem).strip()
    s = re.sub(r"^(copy of\s+)+", "", s, flags=re.I)
    s = _DUP_SUFFIX.sub("", s)
    s = s.lstrip("{[(◾•·-–— ").strip()
    return s


def slugify(text: str, max_len: int = 60) -> str:
    """Lowercase Latin slug: transliterated, accents folded, emoji and punctuation gone."""
    s = transliterate(text)
    # Ligatures NFKD leaves whole (Cœur, æther, Straße) — spelt out, not dropped.
    for a, b in (("œ", "oe"), ("Œ", "Oe"), ("æ", "ae"), ("Æ", "Ae"), ("ß", "ss")):
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("&", " and ").replace("'", "").replace("’", "")
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    s = re.sub(r"-{2,}", "-", s)
    if len(s) > max_len:
        cut = s[:max_len]
        s = cut.rsplit("-", 1)[0] if "-" in cut[max_len // 2:] else cut
        s = s.strip("-")
    return s


def detect_language(text: str) -> str | None:
    declared = _LANG_LINE.search(text)
    if declared:
        code = _LANG_NAMES.get(declared.group(1).lower())
        if code:
            return code
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 2:
        return None
    cyr = [c for c in letters if "Ѐ" <= c <= "ӿ"]
    if len(cyr) / len(letters) > 0.3:
        return "uk" if sum(c.lower() in "іїєґ" for c in cyr) >= 2 else "ru"
    words = re.findall(r"[a-zà-ÿ'’]+", text.lower())
    fr = (sum(w in _FR for w in words) + sum(1 for c in text if c in "éèêàçùâîôûœ")
          + 2 * len(_FR_ELISION.findall(text)))
    en = sum(w in _EN for w in words)
    if fr == en == 0:
        # Latin script with no French signal at all (no elision, no accent, no French word):
        # English is the right guess far more often than not ("Hospital for Souls").
        return "en"
    return "fr" if fr > en else "en"


def detect_year(text: str) -> int | None:
    for m in _YEAR_LINE.finditer(text):
        y = _YEAR.search(m.group(1))
        if y:
            return int(y.group(1))
    return None


def swap_words(slug: str, words: dict[str, str]) -> str:
    parts = slug.split("-")
    return "-".join(words.get(p, p) for p in parts)


# ── the map ────────────────────────────────────────────────────────────────────

@dataclass
class RenameMap:
    folders: dict[str, str] = field(default_factory=dict)  # exact old rel path → new rel path
    names: dict[str, str] = field(default_factory=dict)  # any dir segment → new segment
    files: dict[str, str] = field(default_factory=dict)  # exact file name → new name
    keep: list[str] = field(default_factory=list)  # file-name globs never renamed
    words: dict[str, str] = field(default_factory=dict)  # whole-word swaps inside new names
    tag_globs: list[str] = field(default_factory=list)  # text files that get <lang>[-<year>]--
    lang_globs: dict[str, str] = field(default_factory=dict)  # glob → language the folder states
    exclude: list[str] = field(default_factory=list)  # never renamed (still rewritten)
    rewrite_exclude: list[str] = field(default_factory=list)  # never rewritten (audit logs)
    meta: dict[str, dict[str, Any]] = field(default_factory=dict)  # old rel path → {lang, year}
    source: Path | None = None  # the map file itself — data about the OLD names, never rewritten

    @classmethod
    def load(cls, path: Path | None, root: Path) -> "RenameMap":
        if path is None:
            return cls()
        import yaml

        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        m = cls(
            folders={_norm(k): _norm(v) for k, v in (data.get("folders") or {}).items()},
            names=dict(data.get("names") or {}), files=dict(data.get("files") or {}),
            keep=list(data.get("keep") or []), words=dict(data.get("words") or {}),
            tag_globs=list((data.get("tag") or {}).get("globs") or data.get("tag_globs") or []),
            lang_globs=dict((data.get("tag") or {}).get("lang_globs") or {}),
            exclude=list(data.get("exclude") or []),
            rewrite_exclude=list(data.get("rewrite_exclude") or []),
            source=Path(path).resolve(),
        )
        meta_csv = (data.get("tag") or {}).get("meta_csv")
        if meta_csv:
            p = Path(meta_csv)
            p = p if p.is_absolute() else Path(path).parent / p
            with p.open(encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    key = _norm(row.get("path", ""))
                    if key:
                        m.meta[key] = {"lang": row.get("lang") or None,
                                       "year": int(row["year"]) if (row.get("year") or "").isdigit() else None}
        return m


def _norm(p: str) -> str:
    return str(PurePosixPath(str(p).replace("\\", "/"))).strip("/")


def _match(rel: str, globs: Iterable[str]) -> bool:
    """fnmatch, plus ``dir/**`` meaning the folder itself and everything under it."""
    for g in globs:
        if fnmatch.fnmatch(rel, g):
            return True
        if g.endswith("/**") and (rel == g[:-3] or rel.startswith(g[:-3] + "/")):
            return True
    return False


def _is_link(p: Path) -> bool:
    """A symlink or a Windows junction — never walked into."""
    try:
        if p.is_symlink():
            return True
        st = os.lstat(p)
        return bool(getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return True


def walk(root: Path, exclude: list[str]) -> tuple[list[str], list[str]]:
    """Relative posix paths of directories and files under ``root`` (links skipped)."""
    dirs: list[str] = []
    files: list[str] = []
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            p = Path(e.path)
            rel = p.relative_to(root).as_posix()
            # The tool's own folder (plans, manifests, backups) is data about the old names —
            # never renamed, never rewritten, never counted as a leftover.
            if _is_link(p) or rel == TOOL_DIR or rel.startswith(TOOL_DIR + "/") or _match(rel, exclude):
                continue
            if e.is_dir(follow_symlinks=False):
                dirs.append(rel)
                stack.append(p)
            else:
                files.append(rel)
    return sorted(dirs), sorted(files)


# ── plan ───────────────────────────────────────────────────────────────────────

@dataclass
class Entry:
    kind: str  # dir | file
    src: str
    dst: str
    lang: str = ""
    year: str = ""
    reason: str = ""

    @property
    def changes(self) -> bool:
        return self.src != self.dst


def _read_head(path: Path, limit: int = 20000) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def plan(root: Path, rmap: RenameMap) -> list[Entry]:
    root = Path(root)
    dirs, files = walk(root, rmap.exclude)
    new_dir: dict[str, str] = {"": ""}
    entries: list[Entry] = []
    taken: dict[str, str] = {}  # lower(dst) → src — collision guard on case-insensitive disks

    def claim(dst: str, src: str, is_file: bool) -> str:
        base, n = dst, 2
        head, _, leaf = base.rpartition("/")
        stem, dot, ext = leaf.rpartition(".") if is_file else (leaf, "", "")
        while dst.lower() in taken and taken[dst.lower()] != src:
            name = f"{stem}-{n}.{ext}" if (is_file and dot) else f"{leaf}-{n}"
            dst = f"{head}/{name}" if head else name
            n += 1
        taken[dst.lower()] = src
        return dst

    for rel in dirs:  # sorted → parents first
        parent = rel.rpartition("/")[0]
        seg = rel.rpartition("/")[2]
        if rel in rmap.folders:
            new = rmap.folders[rel]
            reason = "map:folder"
        else:
            name = rmap.names.get(seg)
            reason = "map:name" if name else "slug"
            if not name:
                name = swap_words(slugify(clean_title(seg)) or "untitled", rmap.words)
                if seg.startswith("_") and not name.startswith("_"):
                    name = "_" + name  # a leading underscore marks an auxiliary folder — keep it
            elif rmap.words:
                name = swap_words(name, rmap.words)
            base = new_dir.get(parent, parent)
            new = f"{base}/{name}" if base else name
        new = claim(new, rel, False)
        new_dir[rel] = new
        entries.append(Entry("dir", rel, new, reason=reason if new != rel else "same"))

    for rel in files:
        parent, _, name = rel.rpartition("/")
        base = new_dir.get(parent, parent)
        lang = year = ""
        if name in rmap.files:
            new_name, reason = rmap.files[name], "map:file"
        elif not has_non_latin(name) and any(fnmatch.fnmatch(name, k) for k in rmap.keep):
            # Kept names are not re-slugged, but word swaps (kru → squad) still apply.
            stem, dot, ext = name.rpartition(".")
            new_name = f"{swap_words(stem, rmap.words)}.{ext}" if dot else swap_words(name, rmap.words)
            reason = "keep" if new_name == name else "keep+words"
        else:
            stem, dot, ext = name.rpartition(".")
            if not dot:
                stem, ext = name, ""
            slug = swap_words(slugify(clean_title(stem)) or "untitled", rmap.words)
            reason = "slug"
            if _match(rel, rmap.tag_globs) and f".{ext.lower()}" in TEXT_EXT:
                meta = rmap.meta.get(rel, {})
                head = _read_head(root / rel)
                by_folder = next((lg for g, lg in rmap.lang_globs.items() if _match(rel, [g])), None)
                lang = meta.get("lang") or by_folder or detect_language(stem + "\n" + head) or "xx"
                y = meta.get("year") or detect_year(head)
                year = str(y) if y else ""
                slug = f"{lang}{'-' + year if year else ''}--{slug}"
                reason = "tagged"
            new_name = f"{slug}.{ext.lower()}" if ext else slug
        new = claim(f"{base}/{new_name}" if base else new_name, rel, True)
        entries.append(Entry("file", rel, new, lang, year, reason if new != rel else "same"))
    return entries


def write_plan(entries: list[Entry], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["kind", "src", "dst", "lang", "year", "reason"])
        for e in entries:
            if e.changes:
                w.writerow([e.kind, e.src, e.dst, e.lang, e.year, e.reason])
    return path


def read_plan(path: Path) -> list[Entry]:
    with Path(path).open(encoding="utf-8", newline="") as fh:
        return [Entry(r["kind"], r["src"], r["dst"], r.get("lang", ""), r.get("year", ""), r.get("reason", ""))
                for r in csv.DictReader(fh)]


def summarize(entries: list[Entry]) -> dict[str, Any]:
    moved = [e for e in entries if e.changes]
    by_reason: dict[str, int] = {}
    for e in moved:
        by_reason[e.reason] = by_reason.get(e.reason, 0) + 1
    langs: dict[str, int] = {}
    for e in moved:
        if e.lang:
            langs[e.lang] = langs.get(e.lang, 0) + 1
    return {
        "dirs": sum(1 for e in moved if e.kind == "dir"),
        "files": sum(1 for e in moved if e.kind == "file"),
        "unchanged": len(entries) - len(moved),
        "by_reason": by_reason, "languages": langs,
        "with_year": sum(1 for e in moved if e.year),
        "non_latin_left": sorted({e.dst for e in entries if has_non_latin(e.dst)})[:20],
    }


# ── apply ──────────────────────────────────────────────────────────────────────

def md5(path: Path) -> str:
    h = hashlib.md5()
    with _long(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _long(p: Path) -> Path:
    """Windows long-path prefix for paths past MAX_PATH (Notion names get long)."""
    s = str(p)
    if os.name == "nt" and len(s) > 240 and not s.startswith("\\\\?\\"):
        return Path("\\\\?\\" + os.path.abspath(s))
    return p


def _move(src: Path, dst: Path) -> None:
    _long(dst.parent).mkdir(parents=True, exist_ok=True)
    if str(src).lower() == str(dst).lower() and str(src) != str(dst):  # a case-only rename
        tmp = src.with_name(src.name + ".__renaming__")
        os.replace(_long(src), _long(tmp))
        os.replace(_long(tmp), _long(dst))
    else:
        if _long(dst).exists():
            raise RenameError(f"destination exists, refusing to overwrite: {dst}")
        shutil.move(str(_long(src)), str(_long(dst)))


def _forms(rel: str, root: Path) -> list[str]:
    """Every way one root-relative path can be written in a text file, most specific first."""
    back = rel.replace("/", "\\")
    root_b = str(root).replace("/", "\\")
    absolute = f"{root_b}\\{back}"
    return [
        json.dumps(absolute)[1:-1],  # absolute, JSON with \uXXXX escapes
        json.dumps(absolute, ensure_ascii=False)[1:-1],  # absolute, JSON-escaped backslashes
        absolute,  # absolute, Windows
        f"{root.as_posix()}/{rel}",  # absolute, forward slashes
        json.dumps(back)[1:-1],  # relative, JSON-escaped backslashes
        back,  # relative, backslashes (CSV catalogs)
        rel,  # relative, forward slashes
    ]


def _path_variants(rel: str, root: Path) -> list[str]:
    """The distinct written forms of ``rel`` (used by verify to look for leftovers)."""
    return list(dict.fromkeys(_forms(rel, root)))


def _form_pairs(rel_old: str, rel_new: str, root: Path) -> list[tuple[str, str]]:
    """(old form, new form) in the same encoding — an escaped path stays escaped."""
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for old, new in zip(_forms(rel_old, root), _forms(rel_new, root)):
        if old not in seen and old != new:
            seen.add(old)
            pairs.append((old, new))
    return pairs


_MD_LINK = re.compile(r"(\]\()([^)\s]+)(\))")
_REL_TOKEN = re.compile(r"""((?:\.\.?/)+[^\s"'`)\]|,;]+)""")
# A quoted path relative to the file with no ./ prefix — `image="footage/a.mp4"` in a shot list.
# Quotes are required: unquoted prose like "see footage/a.mp4" is not a path.
_BARE_TOKEN = re.compile(r"""(?<=["'`])([^\s"'`./\\][^\s"'`]*/[^\s"'`]*\.\w{1,5})(?=["'`])""")
# A root-relative or absolute form only counts when it starts a path: not preceded by another
# path character. Without this, `songs/x.mp3` matched again inside `.media/songs/x.mp3` — the
# path it had just been rewritten to — and became `.media/.media/songs/x.mp3`.
_NOT_MID_PATH = r"(?<![\w\-./\\])"


def rewrite_text(text: str, *, file_old: str, file_new: str, mapping: dict[str, str],
                 root: Path, old_exists: set[str]) -> tuple[str, list[tuple[str, str]]]:
    """Rewrite references in one text file. Returns the new text and (old, new) pairs."""
    changes: list[tuple[str, str]] = []
    old_dir = PurePosixPath(file_old).parent
    new_dir = PurePosixPath(file_new).parent

    def map_rel(target_old: str) -> str | None:
        if target_old in mapping:
            return mapping[target_old]
        # a path inside a renamed folder
        parts = target_old.split("/")
        for i in range(len(parts) - 1, 0, -1):
            head = "/".join(parts[:i])
            if head in mapping:
                return mapping[head] + "/" + "/".join(parts[i:])
        return None

    def relink(token: str) -> str | None:
        raw = unquote(token)
        anchor = ""
        if "#" in raw:
            raw, anchor = raw.split("#", 1)
            anchor = "#" + anchor
        if not raw or re.match(r"^[a-z]+:", raw) or raw.startswith("/"):
            return None
        target = os.path.normpath(str(old_dir / raw)).replace("\\", "/")
        if target.startswith(".."):
            return None
        if target not in old_exists:
            return None
        new_target = map_rel(target) or target
        rel = os.path.relpath(new_target, str(new_dir) if str(new_dir) != "." else ".").replace("\\", "/")
        if raw.startswith("./") and not rel.startswith("."):
            rel = "./" + rel
        if rel == raw:
            return None
        return quote(rel, safe="/.-_~") + anchor if "%" in token else rel + anchor

    def sub_md(m: re.Match) -> str:
        new = relink(m.group(2))
        if new is None:
            return m.group(0)
        changes.append((m.group(2), new))
        return m.group(1) + new + m.group(3)

    text = _MD_LINK.sub(sub_md, text)

    def sub_rel(m: re.Match) -> str:
        tok = m.group(1)
        new = relink(tok)
        if new is None:
            return tok
        changes.append((tok, new))
        return new

    text = _REL_TOKEN.sub(sub_rel, text)
    text = _BARE_TOKEN.sub(sub_rel, text)

    # Root-relative and absolute forms, longest paths first so a folder never pre-empts a file.
    for old in sorted(mapping, key=len, reverse=True):
        new = mapping[old]
        if old == new:
            continue
        for variant, repl in _form_pairs(old, new, root):
            if variant not in text:
                continue
            # An absolute form carries the root, so it can never be the tail of its own rewrite.
            lead = "" if re.match(r"^[A-Za-z]:", variant) else _NOT_MID_PATH
            pattern = re.compile(lead + re.escape(variant) + r"(?![\w\-])")
            text, n = pattern.subn(lambda _m, r=repl: r, text)
            if n:
                changes.append((variant, repl))
    return text, changes


@dataclass
class ApplyResult:
    moved: int = 0
    rewritten_files: int = 0
    rewrites: int = 0
    log_dir: Path | None = None
    errors: list[str] = field(default_factory=list)
    left_behind: list[str] = field(default_factory=list)  # empty old folders Windows kept locked


def apply(root: Path, entries: list[Entry], log_dir: Path, rmap: RenameMap) -> ApplyResult:
    root = Path(root)
    log_dir.mkdir(parents=True, exist_ok=True)
    res = ApplyResult(log_dir=log_dir)
    moves = [e for e in entries if e.kind == "file" and e.changes]
    dir_map = {e.src: e.dst for e in entries if e.kind == "dir" and e.changes}
    mapping = {**dir_map, **{e.src: e.dst for e in moves}}

    # Everything that existed before, for resolving relative links from their OLD location.
    all_dirs, all_files = walk(root, [])
    old_exists = set(all_dirs) | set(all_files)
    file_new = {e.src: e.dst for e in moves}

    manifest = log_dir / "rename-manifest.csv"
    with manifest.open("a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        if fh.tell() == 0:
            w.writerow(["ts", "kind", "src", "dst", "md5", "bytes"])
        for e in moves:
            src, dst = root / e.src, root / e.dst
            try:
                digest = md5(src)
                size = _long(src).stat().st_size
                _move(src, dst)
                if md5(dst) != digest:
                    raise RenameError(f"MD5 changed moving {e.src}")
                w.writerow([datetime.now().isoformat(timespec="seconds"), "file", e.src, e.dst, digest, size])
                res.moved += 1
            except (OSError, RenameError) as exc:
                res.errors.append(f"{e.src}: {exc}")
        # Folders that held no files still need to exist under their new name.
        for e in entries:
            if e.kind == "dir" and e.changes and not (root / e.dst).exists():
                (root / e.dst).mkdir(parents=True, exist_ok=True)
                w.writerow([datetime.now().isoformat(timespec="seconds"), "dir", e.src, e.dst, "", ""])

        # Folders whose new name differs only in letter case. On a case-insensitive disk the
        # "new" folder already existed — it IS the old one — so the files moved into it and
        # the folder kept its old spelling. Rename it (parents first) through a temp name.
        for e in sorted((e for e in entries if e.kind == "dir" and e.changes
                         and e.src.lower() == e.dst.lower()), key=lambda e: e.dst.count("/")):
            parent_rel, _, leaf = e.dst.rpartition("/")
            parent = root / parent_rel if parent_rel else root
            try:
                on_disk = next((c.name for c in parent.iterdir() if c.name.lower() == leaf.lower()), None)
                if on_disk and on_disk != leaf:
                    tmp = parent / f"{leaf}.__case__"
                    os.replace(_long(parent / on_disk), _long(tmp))
                    os.replace(_long(tmp), _long(parent / leaf))
                    w.writerow([datetime.now().isoformat(timespec="seconds"), "dircase", e.src, e.dst, "", ""])
            except OSError as exc:
                res.errors.append(f"{e.src}: case rename failed: {exc}")

    # Remove old folders that are now empty (deepest first). Windows can hold a just-emptied
    # folder for a moment (indexer, antivirus, an open Explorer window), so retry briefly and
    # report what stays — an empty old folder left behind is visible, never silent.
    for d in sorted(dir_map, key=lambda s: s.count("/"), reverse=True):
        p = root / d
        if dir_map[d].lower() == d.lower():
            continue  # a case-only rename: the folder is the new one
        for attempt in range(6):
            try:
                if p.exists() and not any(p.iterdir()):
                    p.rmdir()
                break
            except OSError:
                if attempt == 5:
                    res.left_behind.append(d)
                else:
                    time.sleep(0.5)

    # Rewrite references in every text file of the tree (renamed or not), backing each up.
    backup = log_dir / "refs-backup"
    refs_log = log_dir / "refs-rewritten.csv"
    new_dirs, new_files = walk(root, [])
    inverse = {v: k for k, v in file_new.items()}
    with refs_log.open("a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        if fh.tell() == 0:
            w.writerow(["file", "old", "new"])
        for rel in new_files:
            if Path(rel).suffix.lower() not in TEXT_EXT or _match(rel, rmap.rewrite_exclude):
                continue
            if _inside(log_dir, root) and rel.startswith(log_dir.relative_to(root).as_posix() + "/"):
                continue
            path = root / rel
            if rmap.source is not None and path.resolve() == rmap.source:
                continue  # the rename map describes the old names on purpose
            try:
                text = _long(path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            new_text, changes = rewrite_text(text, file_old=inverse.get(rel, rel), file_new=rel,
                                             mapping=mapping, root=root, old_exists=old_exists)
            if new_text == text:
                continue
            bpath = backup / rel
            bpath.parent.mkdir(parents=True, exist_ok=True)
            bpath.write_text(text, encoding="utf-8")
            _long(path).write_text(new_text, encoding="utf-8")
            res.rewritten_files += 1
            res.rewrites += len(changes)
            for old, new in changes:
                w.writerow([rel, old, new])
    (log_dir / "apply.json").write_text(json.dumps({
        "root": str(root), "moved": res.moved, "rewritten_files": res.rewritten_files,
        "rewrites": res.rewrites, "errors": res.errors, "left_behind": res.left_behind,
        "at": datetime.now().isoformat(timespec="seconds"),
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    return res


def _inside(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


# ── verify & undo ──────────────────────────────────────────────────────────────

def read_manifest(log_dir: Path) -> list[dict[str, str]]:
    path = log_dir / "rename-manifest.csv"
    if not path.exists():
        raise RenameError(f"no manifest in {log_dir}")
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def verify(root: Path, log_dir: Path, rmap: RenameMap) -> dict[str, Any]:
    root = Path(root)
    rows = read_manifest(log_dir)
    rewritten: set[str] = set()
    refs = log_dir / "refs-rewritten.csv"
    if refs.exists():
        with refs.open(encoding="utf-8", newline="") as fh:
            rewritten = {r["file"] for r in csv.DictReader(fh)}
    missing, stale, bad_md5 = [], [], []
    for r in rows:
        if r["kind"] != "file":
            continue
        dst, src = root / r["dst"], root / r["src"]
        if not _long(dst).exists():
            missing.append(r["dst"])
        elif r["dst"] not in rewritten and md5(dst) != r["md5"]:
            # A text file whose references were rewritten changed on purpose (its original is
            # in refs-backup/); every other file must be byte-identical.
            bad_md5.append(r["dst"])
        if _long(src).exists() and src.as_posix().lower() != dst.as_posix().lower():
            stale.append(r["src"])
    dirs, files = walk(root, rmap.exclude)
    non_latin = [p for p in dirs + files if has_non_latin(p)]
    on_disk = set(dirs) | set(files)
    lowered = {p.lower() for p in on_disk}
    wrong_case = sorted({r["dst"] for r in rows if r["kind"] in ("file", "dir", "dircase")
                         and r["dst"] not in on_disk and r["dst"].lower() in lowered})
    old_paths = sorted({r["src"] for r in rows if has_non_latin(r["src"])}, key=len, reverse=True)
    hits: list[str] = []
    _, all_files = walk(root, [])
    probes = [v for old in old_paths[:4000] for v in _path_variants(old, root)]
    for rel in all_files:
        if Path(rel).suffix.lower() not in TEXT_EXT or _match(rel, rmap.rewrite_exclude):
            continue
        if _inside(log_dir, root) and rel.startswith(log_dir.relative_to(root).as_posix() + "/"):
            continue
        try:
            text = _long(root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for v in probes:
            if v in text:
                hits.append(f"{rel}: {v}")
                break
    return {
        "files": sum(1 for r in rows if r["kind"] == "file"),
        "missing": missing, "stale_sources": stale, "md5_mismatch": bad_md5,
        "non_latin_paths": non_latin[:50], "non_latin_count": len(non_latin),
        "old_path_references": hits[:50], "old_path_reference_count": len(hits),
        "wrong_case": wrong_case[:50], "wrong_case_count": len(wrong_case),
        "ok": not (missing or stale or bad_md5 or non_latin or hits or wrong_case),
    }


def undo(root: Path, log_dir: Path) -> dict[str, int]:
    root = Path(root)
    restored = moved = 0
    backup = log_dir / "refs-backup"
    rows = [r for r in read_manifest(log_dir) if r["kind"] == "file"]
    # Text files were rewritten after the moves; restore them where they are now first.
    if backup.exists():
        for b in backup.rglob("*"):
            if b.is_file():
                rel = b.relative_to(backup).as_posix()
                _long(root / rel).write_text(b.read_text(encoding="utf-8"), encoding="utf-8")
                restored += 1
    for r in reversed(rows):
        dst, src = root / r["dst"], root / r["src"]
        if _long(dst).exists() and (not _long(src).exists() or str(src).lower() == str(dst).lower()):
            if str(src) != str(dst):
                _move(dst, src)
                moved += 1
    for r in sorted((r for r in read_manifest(log_dir) if r["kind"] == "dircase"),
                    key=lambda r: r["dst"].count("/"), reverse=True):
        parent_rel, _, leaf = r["src"].rpartition("/")
        parent = root / parent_rel if parent_rel else root
        cur = next((c for c in parent.iterdir() if c.name.lower() == leaf.lower()), None) if parent.exists() else None
        if cur is not None and cur.name != leaf:
            tmp = parent / f"{leaf}.__case__"
            os.replace(_long(cur), _long(tmp))
            os.replace(_long(tmp), _long(parent / leaf))
    for r in sorted((r for r in read_manifest(log_dir) if r["kind"] == "dir"),
                    key=lambda r: r["dst"].count("/"), reverse=True):
        p = root / r["dst"]
        if p.exists() and not any(p.iterdir()):
            p.rmdir()
    # The folders the files moved out of are empty now; prune them deepest-first.
    for r in sorted(rows, key=lambda r: r["dst"].count("/"), reverse=True):
        p = (root / r["dst"]).parent
        while p != root and p.exists() and not any(p.iterdir()):
            p.rmdir()
            p = p.parent
    return {"restored_text_files": restored, "moved_back": moved}
