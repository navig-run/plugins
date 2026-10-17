"""Plan a folder tree for a bookmark library with an AI — and refuse a bad plan.

The pipeline, each step a pure function so it can be tested without an AI:

1. :func:`build_units` — local pre-grouping. A site with many bookmarks (``group_min``+) is
   sent as ONE unit ("37 bookmarks on python.org: …"), so the AI places the group once
   instead of 37 times. Multi-topic platforms (GitHub, YouTube, Reddit, Wikipedia…) are
   never grouped: their bookmarks share a host, not a subject.
2. :func:`batch_units` — batches sized by characters and count, so a 10 000-bookmark library
   is many small, reliable calls rather than one that truncates.
3. :func:`build_prompt` — strict JSON contract; the folders created by earlier batches are
   passed to later ones so the tree stays consistent across batches.
4. :func:`parse_assignments` / :func:`sanitize_folder` — strict parse (with truncated-JSON
   repair, :mod:`navig_bookmarks.ai_json`), unknown ids collected, depth clamped.
5. :func:`plan_organize` — runs the batches; ids the AI skipped get ONE retry.
6. :func:`validate_plan` — every id assigned, no invented ids, depth within the limit.
7. :func:`failsafe` — a plan that moves more than 20 % of the bookmarks or makes more than
   1 000 changes needs ``--force`` to apply.
8. :func:`diff_rows` — what changes, folder by folder.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from navig_bookmarks.ai_json import AIResponseError, parse_ai_json
from navig_bookmarks.models import Bookmark, folder_depth, join_folder, split_folder
from navig_bookmarks.urls import site_of

MAX_DEPTH = 3
FAILSAFE_MAX_MOVE_RATIO = 0.20
FAILSAFE_MAX_CHANGES = 1000

#: Sites whose bookmarks span every subject; grouping them by host would be wrong.
PLATFORM_SITES = frozenset({
    "github.com", "gitlab.com", "bitbucket.org", "youtube.com", "youtu.be", "medium.com",
    "reddit.com", "twitter.com", "x.com", "stackoverflow.com", "stackexchange.com",
    "wikipedia.org", "google.com", "amazon.com", "substack.com", "dev.to", "ycombinator.com",
    "notion.so", "notion.site", "archive.org", "linkedin.com", "facebook.com", "instagram.com",
    "pinterest.com", "vimeo.com", "tiktok.com", "quora.com", "apple.com", "microsoft.com",
    "huggingface.co", "arxiv.org", "npmjs.com", "pypi.org", "docs.google.com", "drive.google.com",
})

#: (system, user, max_tokens) -> reply text. The command passes navig_sdk.ai.generate.
Generate = Callable[[str, str, int], str]


@dataclass
class Unit:
    key: str  # a bookmark id, or "g:<site>" for a pre-grouped site
    members: list[str]
    text: str


@dataclass
class PlanResult:
    plan: dict[str, str] = field(default_factory=dict)  # bookmark id -> folder
    invented: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    clamped: int = 0
    batches: int = 0
    calls: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class Failsafe:
    total: int
    moves: int
    new_folders: int
    emptied_folders: int
    changes: int
    ratio: float
    tripped: bool
    reasons: list[str]


def _short(text: str, n: int) -> str:
    t = " ".join(str(text or "").split()).replace("|", "/")
    return t if len(t) <= n else t[: n - 1] + "…"


def build_units(bookmarks: Iterable[Bookmark], *, group_min: int = 5) -> list[Unit]:
    by_site: dict[str, list[Bookmark]] = defaultdict(list)
    for b in bookmarks:
        by_site[site_of(b.url)].append(b)
    units: list[Unit] = []
    for site, items in sorted(by_site.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if group_min > 0 and site and site not in PLATFORM_SITES and len(items) >= group_min:
            sample = "; ".join(_short(b.title or b.url, 48) for b in items[:5])
            units.append(Unit(f"g:{site}", [b.id for b in items], f"{len(items)} bookmarks on {site}: {sample}"))
            continue
        for b in items:
            where = f" | now in: {_short(b.folder_path, 60)}" if b.folder_path else ""
            units.append(Unit(b.id, [b.id], f"{_short(b.title or '(untitled)', 90)} | {site or _short(b.url, 60)}{where}"))
    return units


def batch_units(units: list[Unit], *, max_chars: int = 12000, max_units: int = 150) -> list[list[Unit]]:
    batches: list[list[Unit]] = []
    cur: list[Unit] = []
    size = 0
    for u in units:
        line = len(u.key) + len(u.text) + 4
        if cur and (size + line > max_chars or len(cur) >= max_units):
            batches.append(cur)
            cur, size = [], 0
        cur.append(u)
        size += line
    if cur:
        batches.append(cur)
    return batches


def build_prompt(batch: list[Unit], known_folders: list[str], *, max_depth: int = MAX_DEPTH) -> tuple[str, str]:
    system = (
        "You organise a person's bookmarks into a clean folder tree.\n"
        "Rules:\n"
        '- Reply with ONLY JSON: {"assignments": [{"id": "<id>", "folder": "<Folder/Subfolder>"}]}\n'
        "- Exactly one entry for EVERY id you are given. Never skip an id; never invent one.\n"
        f'- Folder paths use "/" between levels and are at most {max_depth} levels deep.\n'
        '- Short, specific Title Case names ("Development/Python", "Home/Recipes"). No "Misc",\n'
        '  "Other" or "Uncategorized" unless nothing else could possibly fit.\n'
        "- Reuse an existing folder whenever it fits; create a new one only when none does.\n"
        "- Group by what the page is FOR (topic or task), not by website, unless the site is the topic.\n"
        '- An id starting with "g:" stands for several bookmarks on one site: give it one folder.'
    )
    lines = []
    if known_folders:
        lines.append("Existing folders (reuse when they fit):")
        lines += [f"- {f}" for f in known_folders[:150]]
        lines.append("")
    lines.append("Bookmarks (id | title | site | current folder):")
    lines += [f"{u.key} | {u.text}" for u in batch]
    return system, "\n".join(lines)


def _folder_keys(path: str) -> list[str]:
    """The spellings an AI may echo an existing folder back as: stored, displayed, any case."""
    shown = "/".join(split_folder(path))
    return list(dict.fromkeys([path, shown, path.lower(), shown.lower()]))


def folder_lookup(folders: Iterable[str]) -> dict[str, str]:
    """``spelling -> stored path`` for existing folders (see :func:`sanitize_folder`)."""
    out: dict[str, str] = {}
    for f in folders:
        for key in _folder_keys(f):
            out.setdefault(key, f)
    return out


def sanitize_folder(
    folder: str, *, max_depth: int = MAX_DEPTH, known: dict[str, str] | None = None
) -> tuple[str, bool]:
    """Clean an AI folder path; clamp it to *max_depth* levels. Returns ``(path, clamped)``.

    A path that names an existing folder maps back to it EXACTLY — including a folder whose
    name contains "/" ("A/B testing"), which a plain split would cut in two. Otherwise "/" (or
    " > ") separates levels, honouring a backslash-escaped slash inside a name.
    """
    raw = str(folder or "").strip().strip("\"'`").strip()
    if known:
        for key in (raw, raw.lower()):
            hit = known.get(key)
            if hit and folder_depth(hit) <= max_depth:
                return hit, False
    parts = []
    for part in split_folder(re.sub(r"\s*>\s*", "/", raw)):
        name = " ".join(part.strip().strip("\"'`").split())[:60]
        if name:
            parts.append(name)
    clamped = len(parts) > max_depth
    return join_folder(parts[:max_depth]), clamped


def parse_assignments(obj: object, allowed: set[str]) -> tuple[dict[str, str], list[str]]:
    """``{unit_key: folder}`` from a parsed reply, plus the keys the AI made up."""
    rows = obj.get("assignments") if isinstance(obj, dict) else obj
    if not isinstance(rows, list):
        raise AIResponseError('the reply has no "assignments" list')
    got: dict[str, str] = {}
    invented: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("id", "")).strip()
        folder = row.get("folder")
        if key not in allowed:
            if key:
                invented.append(key)
            continue
        if isinstance(folder, str) and folder.strip():
            got[key] = folder
    return got, invented


def _max_tokens(n_units: int) -> int:
    return min(8000, 200 + 40 * n_units)


def plan_organize(
    bookmarks: list[Bookmark],
    generate: Generate,
    *,
    existing_folders: Iterable[str] = (),
    max_depth: int = MAX_DEPTH,
    group_min: int = 5,
    max_chars: int = 12000,
    progress: Callable[[int, int], None] | None = None,
) -> PlanResult:
    """Ask the AI for a folder per bookmark. AI/provider errors propagate (nothing is half-done)."""
    units = build_units(bookmarks, group_min=group_min)
    by_key = {u.key: u for u in units}
    known: list[str] = [f for f in dict.fromkeys(existing_folders) if f]
    lookup = folder_lookup(known)
    unit_plan: dict[str, str] = {}
    result = PlanResult()
    batches = batch_units(units, max_chars=max_chars)
    result.batches = len(batches)
    for n, batch in enumerate(batches, 1):
        if progress:
            progress(n, len(batches))
        pending = batch
        for attempt in range(2):
            system, user = build_prompt(pending, known, max_depth=max_depth)
            result.calls += 1
            reply = generate(system, user, _max_tokens(len(pending)))
            try:
                got, invented = parse_assignments(parse_ai_json(reply), {u.key for u in pending})
            except AIResponseError as exc:
                if attempt == 1:
                    result.errors.append(f"batch {n}: {exc}")
                continue
            result.invented += invented
            for key, folder in got.items():
                clean, clamped = sanitize_folder(folder, max_depth=max_depth, known=lookup)
                if not clean:
                    continue
                result.clamped += int(clamped)
                unit_plan[key] = clean
                if clean not in known:
                    known.append(clean)
                    for spelling in _folder_keys(clean):
                        lookup.setdefault(spelling, clean)
            pending = [u for u in pending if u.key not in unit_plan]
            if not pending:
                break
    for key, folder in unit_plan.items():
        for member in by_key[key].members:
            result.plan[member] = folder
    result.missing = [b.id for b in bookmarks if b.id not in result.plan]
    return result


def validate_plan(
    plan: dict[str, str], ids: Iterable[str], *, max_depth: int = MAX_DEPTH, invented: Iterable[str] = ()
) -> list[str]:
    """Every problem with *plan*; an empty list means it is safe to show and apply."""
    wanted = set(ids)
    errors: list[str] = []
    missing = sorted(wanted - set(plan))
    if missing:
        errors.append(f"{len(missing)} bookmark(s) got no folder: {', '.join(missing[:8])}{' …' if len(missing) > 8 else ''}")
    unknown = sorted(set(plan) - wanted | set(invented))
    if unknown:
        errors.append(f"{len(unknown)} unknown id(s) in the plan: {', '.join(unknown[:8])}{' …' if len(unknown) > 8 else ''}")
    too_deep = sorted({f for f in plan.values() if folder_depth(f) > max_depth})
    if too_deep:
        errors.append(f"{len(too_deep)} folder(s) deeper than {max_depth} levels: {', '.join(too_deep[:5])}")
    empty = [bid for bid, f in plan.items() if folder_depth(f) == 0]
    if empty:
        errors.append(f"{len(empty)} bookmark(s) assigned an empty folder")
    return errors


def failsafe(
    plan: dict[str, str],
    current: dict[str, str],
    *,
    max_ratio: float = FAILSAFE_MAX_MOVE_RATIO,
    max_changes: int = FAILSAFE_MAX_CHANGES,
) -> Failsafe:
    """How big the plan is, and whether it is big enough to need ``--force``."""
    total = len(current) or len(plan)
    moves = sum(1 for bid, folder in plan.items() if current.get(bid, "") != folder)
    after: dict[str, str] = {**current, **plan}
    before_folders = {f for f in current.values() if f}
    after_folders = {f for f in after.values() if f}
    new_folders = len(after_folders - before_folders)
    emptied = len(before_folders - after_folders)
    changes = moves + new_folders + emptied
    ratio = (moves / total) if total else 0.0
    reasons = []
    if ratio > max_ratio:
        reasons.append(f"moves {moves} of {total} bookmarks ({ratio:.0%} > {max_ratio:.0%})")
    if changes > max_changes:
        reasons.append(f"{changes} changes (> {max_changes})")
    return Failsafe(total, moves, new_folders, emptied, changes, ratio, bool(reasons), reasons)


def diff_rows(plan: dict[str, str], current: dict[str, str]) -> list[tuple[str, int, int]]:
    """``(folder, before, after)`` for every folder whose count changes, sorted by folder."""
    before: dict[str, int] = defaultdict(int)
    after: dict[str, int] = defaultdict(int)
    for bid, folder in current.items():
        before[folder] += 1
        after[plan.get(bid, folder)] += 1
    rows = [(f, before.get(f, 0), after.get(f, 0)) for f in sorted(set(before) | set(after))]
    return [r for r in rows if r[1] != r[2]]
