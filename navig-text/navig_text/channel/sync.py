"""Push the master's values into the channel's scripts — positional, never by guessing.

Ported from the workspace's ``.tmp/sync_rev6.py``. For every episode beat,
transmission section, lore entry and caption we take the CURRENT value out of the
script module, take the NEW value out of the master, and replace the old literal in
source — so it works no matter which revision the code was last synced to.

Two rules that survive the port:

* a literal swap must find EXACTLY ONE occurrence — zero or two means we do not know
  what we are editing, and the run reports it instead of writing;
* wholesale rebuilds (transmission blocks, the LORE list) go through a string-aware
  block slicer + a literal printer, because long section strings are line-wrapped in
  source and can never be found by a literal swap.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from navig_text.channel.parse import ParsedMaster
from navig_text.workspace import load_workspace_module

__all__ = [
    "SyncReport",
    "Syncer",
    "block_end",
    "esc",
    "pyrepr",
    "slice_block",
    "slice_list",
    "swap_lit",
]


# ── source surgery ────────────────────────────────────────────────────────────
def block_end(src: str, open_idx: int) -> int:
    """Index just past the ``}`` matching the ``{`` at ``open_idx`` (string-aware)."""
    depth, i, in_s, q, esc_ = 0, open_idx, False, "", False
    while i < len(src):
        c = src[i]
        if in_s:
            if esc_:
                esc_ = False
            elif c == "\\":
                esc_ = True
            elif c == q:
                in_s = False
        elif c in "\"'":
            in_s, q = True, c
        elif c in "{[(":
            depth += 1
        elif c in "}])":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unbalanced")


def slice_block(src: str, start_at: int) -> tuple[int, int]:
    brace = src.index("{", start_at)
    return brace, block_end(src, brace)


def slice_list(src: str, start_at: int) -> tuple[int, int]:
    """(open, end) of the LIST bracket at/after ``start_at``.

    ``sync_rev6.py``'s patch_lore used :func:`slice_block` here — it grabbed the
    first ``{`` INSIDE the list and rebuilt around it, nesting the whole list as the
    first element and leaving a stale tail (the "lore double-list bug": compiles,
    then crashes the next run with ``TypeError: list indices must be integers``).
    ``fix_lore.py`` matched the list bracket instead; this is that fix, kept.
    """
    lb = src.index("[", start_at)
    return lb, block_end(src, lb)


def esc(s: str) -> str:
    """How a python double-quoted literal body looks inside a source file."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def swap_lit(
    text: str, old: str, new: str, scope: str, path: str, report: list[str], stats: dict
) -> str:
    """Replace ``old`` (a python string literal body) with ``new``, exactly once."""
    if old == new:
        stats["same"] += 1
        return text
    o, n = esc(old), esc(new)
    cands = [(f'"{o}"', f'"{n}"')]
    if "'" not in o:
        cands.append((f"'{o}'", f"'{n}'"))
    for pat, rep in cands:
        if text.count(pat) == 1:
            stats["ok"] += 1
            return text.replace(pat, rep)
    cnt = text.count(f'"{o}"') + text.count(f"'{o}'")
    report.append(f"  !! {path} [{scope}] '{o[:44]}…' occurrences={cnt}")
    return text


def swap_raw(
    text: str, old: str, new: str, scope: str, path: str, report: list[str], stats: dict
) -> str:
    """Replace a raw literal (e.g. a list) exactly as written."""
    if old == new:
        stats["same"] += 1
        return text
    n = text.count(old)
    if n == 1:
        stats["ok"] += 1
        return text.replace(old, new)
    report.append(f"  !! {path} [{scope}] raw '{old[:44]}…' occurrences={n}")
    return text


def pyrepr(v, ind: int = 0) -> str:
    """A python literal that keeps tuples tuples and reads decently."""
    pad, pad2 = "    " * ind, "    " * (ind + 1)
    if isinstance(v, str):
        return '"' + esc(v) + '"'
    if isinstance(v, tuple):
        if not v:
            return "()"
        items = ", ".join(pyrepr(x, ind) for x in v)
        return "(" + items + ("," if len(v) == 1 else "") + ")"
    if isinstance(v, list):
        if not v:
            return "[]"
        if all(isinstance(x, str) and "\n" not in x and len(x) < 60 for x in v):
            return "[" + ", ".join(pyrepr(x, ind) for x in v) + "]"
        return (
            "[\n"
            + ",\n".join(pad2 + pyrepr(x, ind + 1) for x in v)
            + "\n"
            + pad
            + "]"
        )
    if isinstance(v, dict):
        if not v:
            return "{}"
        parts = [f"{pyrepr(k, ind + 1)}: {pyrepr(x, ind + 1)}" for k, x in v.items()]
        return "{\n" + ",\n".join(pad2 + p for p in parts) + "\n" + pad + "}"
    return repr(v)


# ── report ────────────────────────────────────────────────────────────────────
@dataclass
class SyncReport:
    """One run's output: the human lines plus the counts the summary line prints."""

    lines: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=lambda: {"ok": 0, "same": 0})
    applied: bool = False
    header: str = ""
    #: files whose content WOULD change (dry) or DID change (apply). ``stats['ok']``
    #: counts tx/lore section updates unconditionally (the original's counting — the
    #: summary line stays byte-compatible with it), so ``--check`` reads THIS instead.
    pending: list[str] = field(default_factory=list)

    @property
    def problems(self) -> int:
        return sum(1 for r in self.lines if r.startswith("  !!"))

    @property
    def wrote(self) -> list[str]:
        return [r.strip() for r in self.lines if r.strip().startswith("wrote ")]

    def summary(self) -> str:
        return (
            ("APPLIED" if self.applied else "DRY")
            + f" — swapped: {self.stats['ok']}, identical: {self.stats['same']},"
            f" problems: {self.problems}"
        )

    def render(self) -> str:
        return "\n".join([self.header, *self.lines, self.summary()])


# ── syncer ────────────────────────────────────────────────────────────────────
_PATCHES = ("episodes", "tx", "lore", "posts", "txlines")


class Syncer:
    """Sync a workspace's scripts to a parsed master.

    ``apply=False`` (default) only reports what WOULD change — identical output to
    the old script's dry run, line for line.
    """

    def __init__(
        self,
        workspace: Path,
        master: ParsedMaster,
        master_path: Path,
        apply: bool = False,
        only: Optional[str] = None,
    ) -> None:
        if only is not None and only not in _PATCHES:
            raise ValueError(f"only must be one of {', '.join(_PATCHES)}")
        self.ws = Path(workspace)
        self.master = master
        self.master_path = Path(master_path)
        self.apply = apply
        self.only = only
        # The old script called everything "rev6" — derive the same label from the
        # filename (ALL-TEXTS-rev6.md → rev6) so reports stay byte-comparable.
        stem = Path(master_path).stem
        self.label = ("rev" + stem.rsplit("-rev", 1)[1]) if "-rev" in stem else stem
        self.report = SyncReport(applied=apply)

    # -- helpers -------------------------------------------------------------
    def _wants(self, patch: str) -> bool:
        # posts and txlines share one source file but are separately switchable;
        # `--only posts` runs the posts half, `--only txlines` the post-lines half.
        return self.only is None or self.only == patch

    def _edit_file(self, path: Path, fn: Callable[[str], str]) -> None:
        src = path.read_text(encoding="utf-8")
        new = fn(src)
        if new != src:
            self.report.pending.append(path.name)
        if self.apply and new != src:
            path.write_text(new, encoding="utf-8", newline="\n")
            self.report.lines.append(f"  wrote {path.name}")
        elif not self.apply:
            self.report.lines.append(f"  (dry) {path.name}")

    def _swap(self, text: str, old: str, new: str, scope: str, path: str) -> str:
        return swap_lit(
            text, old, new, scope, path, self.report.lines, self.report.stats
        )

    def _swap_raw(self, text: str, old: str, new: str, scope: str, path: str) -> str:
        return swap_raw(
            text, old, new, scope, path, self.report.lines, self.report.stats
        )

    def _fail(self, msg: str) -> None:
        self.report.lines.append(f"  !! {msg}")

    # -- patches -------------------------------------------------------------
    def patch_episodes(self) -> None:
        if not self._wants("episodes"):
            return
        EP = load_workspace_module(self.ws, "episodes")

        def work(src: str) -> str:
            for name, data in EP.EPISODES.items():
                rd = self.master.episodes.get(name)
                if not rd:
                    self._fail(f"episodes: {name} missing in {self.label}")
                    continue
                if len(rd["beats"]) != len(data["beats"]):
                    self._fail(
                        f"episodes: {name} beat count {len(data['beats'])} vs"
                        f" {self.label} {len(rd['beats'])} — skipped"
                    )
                    continue
                key = f'"{name}": {{'
                at = src.find(key)
                if at < 0:
                    at = src.find(f'EPISODES["{name}"] = {{')
                    if at < 0:
                        self._fail(f"episodes: block {name} not found")
                        continue
                b0, b1 = slice_block(src, at)
                blk = src[b0:b1]
                for lang, val in rd["end"].items():
                    old = data.get("end", {}).get(lang)
                    if old:
                        blk = self._swap(blk, old, val, f"{name}.end.{lang}", "episodes.py")
                for i, (b, rb) in enumerate(zip(data["beats"], rd["beats"])):
                    say = b.get("say") or {}
                    for lang, new in rb["lines"].items():
                        old = say.get(lang)
                        if old is None:
                            if lang == "fr":
                                continue
                            self._fail(f"episodes {name}[{i}] no {lang} in code")
                            continue
                        blk = self._swap(
                            blk, old, new, f"{name}[{i}].{lang}", "episodes.py"
                        )
                src = src[:b0] + blk + src[b1:]
            return src

        self._edit_file(self.ws / "episodes.py", work)

    def patch_transmission(self) -> None:
        if not self._wants("tx"):
            return
        TR = load_workspace_module(self.ws, "transmission")

        def work(src: str) -> str:
            # entries rebuilt wholesale: long section strings are line-wrapped in
            # source, so a literal swap can never find them
            for pid, data in TR.PAGES.items():
                rd = self.master.tx.get(pid)
                if not rd:
                    self._fail(f"tx {pid} missing in {self.label}")
                    continue
                at = src.find(f'"{pid}": {{')
                if at < 0:
                    self._fail(f"tx block {pid} not found")
                    continue
                b0, b1 = slice_block(src, at)
                entry = copy.deepcopy(data)
                for lang in ("en", "ru"):
                    if lang not in entry or lang not in rd:
                        continue
                    if rd[lang]["head"]:
                        entry[lang]["head"] = rd[lang]["head"]
                    cs = entry[lang].get("sections") or []
                    rs = rd[lang]["sections"]
                    if len(cs) != len(rs):
                        self._fail(
                            f"tx {pid}.{lang}: {len(cs)} sections vs {len(rs)} in"
                            f" {self.label} — kept code texts"
                        )
                        continue
                    entry[lang]["sections"] = [
                        (cl, rt) for (cl, _ct), (_rl, rt) in zip(cs, rs)
                    ]
                    self.report.stats["ok"] += 1
                # rebuild in BOTH modes — a dry run must see what apply would write
                src = src[:b0] + pyrepr(entry, 0) + src[b1:]
            return src

        self._edit_file(self.ws / "transmission.py", work)

    def patch_lore(self) -> None:
        if not self._wants("lore"):
            return
        LT = load_workspace_module(self.ws, "lore_texts")

        def work(src: str) -> str:
            at = src.find("LORE = [")
            if at < 0:
                self._fail("lore: LORE = [ not found")
                return src
            b0, b1 = slice_list(src, at)
            entries = []
            for i, entry in enumerate(LT.LORE):
                if not isinstance(entry, dict):
                    # residue of the double-list bug: LORE holds a nested list
                    self._fail(
                        f"lore entry {i} is {type(entry).__name__}, not a dict —"
                        " lore_texts.py is corrupted; fix it before syncing"
                    )
                    return src
                e = copy.deepcopy(entry)
                rd = self.master.lore.get(e.get("id", ""))
                if not rd:
                    self._fail(f"lore {e.get('id')} missing in {self.label}")
                else:
                    for lang in ("en", "ru"):
                        if lang in rd:
                            e[lang] = rd[lang]
                            self.report.stats["ok"] += 1
                entries.append(e)
            body = ",\n".join("    " + pyrepr(e, 1) for e in entries)
            # rebuild in BOTH modes — a dry run must see what apply would write
            src = src[:b0] + "[\n" + body + "\n]" + src[b1:]
            return src

        self._edit_file(self.ws / "lore_texts.py", work)

    def patch_posts(self) -> None:
        """Publish-kit posts AND transmission post-lines (same source file)."""
        if not (self._wants("posts") or self._wants("txlines")):
            return
        PK = load_workspace_module(self.ws, "publish_kit")
        want_posts, want_tx = self._wants("posts"), self._wants("txlines")

        def work(src: str) -> str:
            if want_posts:
                for name, fields in self.master.posts.items():
                    cur = PK.POSTS.get(name)
                    if not cur:
                        self._fail(f"post {name} missing in publish_kit")
                        continue
                    for field_, langs in fields.items():
                        cur_field = cur.get(field_)
                        if cur_field is None:
                            self._fail(f"post {name}.{field_} missing in code")
                            continue
                        if field_ == "tags":
                            at = src.find(f'"{name}": {{')
                            if at < 0:
                                self._fail(f"post block {name} not found")
                                continue
                            b0, b1 = slice_block(src, at)
                            blk = src[b0:b1]
                            for lang, val in langs.items():
                                old = list(cur_field.get(lang, ()))
                                new = [t.lstrip("#") for t in val.split()]
                                if old == new:
                                    self.report.stats["same"] += 1
                                    continue
                                for q in ('"', "'"):
                                    old_l = "[" + ", ".join(f"{q}{t}{q}" for t in old) + "]"
                                    new_l = "[" + ", ".join(f"{q}{t}{q}" for t in new) + "]"
                                    if old_l in blk:
                                        blk = self._swap_raw(
                                            blk, old_l, new_l,
                                            f"{name}.tags.{lang}", "publish_kit.py",
                                        )
                                        cur_field[lang] = new
                                        break
                                else:
                                    self._fail(
                                        f"post {name}.tags.{lang} literal not found: {old}"
                                    )
                            src = src[:b0] + blk + src[b1:]
                            continue
                        for lang, val in langs.items():
                            old = cur_field.get(lang)
                            if old is None:
                                continue  # fr not in code for this post
                            src = self._swap(
                                src, old, val, f"{name}.{field_}.{lang}", "publish_kit.py"
                            )
            if want_tx:
                for pid, langs in self.master.txlines.items():
                    cur = PK.TX_POSTS.get(pid)
                    if not cur:
                        self._fail(f"txline {pid} missing")
                        continue
                    for lang, val in langs.items():
                        if lang in cur:
                            src = self._swap(
                                src, cur[lang], val, f"tx{pid}.{lang}", "publish_kit.py"
                            )
            return src

        self._edit_file(self.ws / "publish_kit.py", work)

    # -- run -----------------------------------------------------------------
    def run(self) -> SyncReport:
        m = self.master
        self.report.header = (
            f"{self.label}: {len(m['episodes'])} episodes,"
            f" {len(m['tx'])} transmissions, {len(m['lore'])} lore,"
            f" {len(m['posts'])} posts, {len(m['txlines'])} tx lines"
        )
        self.patch_episodes()
        self.patch_transmission()
        self.patch_lore()
        self.patch_posts()
        return self.report
