"""AI tags: few, durable, worth searching for — and only ever replacing the AI's own tags.

The prompt asks for tags a person would actually search with later (a topic or a purpose,
not the site's name or "article"), steers the model to reuse the existing vocabulary so the
tag list does not sprawl, and in ``curated`` mode allows ONLY tags from that vocabulary.
Results are stored with source ``ai`` through
:meth:`navig_bookmarks.store.BookmarkStore.replace_source_tags`, so a re-run swaps the
previous AI tags and never touches a tag a person or a rule added.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from navig_bookmarks.ai_json import AIResponseError, parse_ai_json
from navig_bookmarks.models import Bookmark
from navig_bookmarks.store import clean_tag
from navig_bookmarks.urls import site_of

#: Words that tag nothing a person would search for.
JUNK_TAGS = frozenset({
    "website", "web", "site", "link", "links", "bookmark", "bookmarks", "page", "webpage", "article",
    "misc", "other", "general", "stuff", "interesting", "useful", "read", "todo", "online", "internet",
    "homepage", "home-page", "untitled", "error", "login",
})

Generate = Callable[[str, str, int], str]


@dataclass
class TagResult:
    tags: dict[str, list[str]] = field(default_factory=dict)  # bookmark id -> tags
    missing: list[str] = field(default_factory=list)
    invented: list[str] = field(default_factory=list)
    calls: int = 0
    errors: list[str] = field(default_factory=list)


def build_prompt(batch: list[Bookmark], vocabulary: list[str], *, curated: bool, max_tags: int) -> tuple[str, str]:
    rules = [
        "You tag bookmarks so the person can find them again later.",
        "Rules:",
        '- Reply with ONLY JSON: {"tags": [{"id": "<id>", "tags": ["tag", "..."]}]} with one entry for EVERY id.',
        f"- 0 to {max_tags} tags each: durable topics or purposes someone would search for",
        '  ("python", "taxes", "recipes", "self-hosting"), not passing details.',
        "- lowercase; one word or a hyphenated phrase.",
        "- Never the site's name on its own, never format words (article, website, link, page),",
        "  dates, or opinions.",
        "- If a title looks like an error, login, captcha or placeholder page, give it [].",
    ]
    if curated:
        rules.append("- Use ONLY tags from the vocabulary below, spelled exactly; [] when none fits.")
    elif vocabulary:
        rules.append("- Prefer tags from the vocabulary below when they fit, spelled exactly; add a new tag only when none fits.")
    lines = []
    if vocabulary:
        lines.append("Vocabulary: " + ", ".join(vocabulary[:300]))
        lines.append("")
    lines.append("Bookmarks (id | title | site | folder | notes):")
    for b in batch:
        notes = " ".join(str(b.notes or "").split())[:120]
        title = " ".join(str(b.title or "(untitled)").split())[:100]
        lines.append(f"{b.id} | {title} | {site_of(b.url) or b.url[:60]} | {b.folder_path or '-'} | {notes or '-'}")
    return "\n".join(rules), "\n".join(lines)


def clean_suggestions(raw: Iterable[object], *, vocabulary: set[str], curated: bool, max_tags: int) -> list[str]:
    out: list[str] = []
    for item in raw:
        tag = clean_tag(str(item))
        if not tag or tag in JUNK_TAGS or tag.isdigit() or len(tag) < 2:
            continue
        if curated and tag not in vocabulary:
            continue
        if tag not in out:
            out.append(tag)
    return out[:max_tags]


def suggest_tags(
    bookmarks: list[Bookmark],
    generate: Generate,
    *,
    vocabulary: list[str],
    curated: bool = False,
    max_tags: int = 4,
    batch_size: int = 40,
    progress: Callable[[int, int], None] | None = None,
) -> TagResult:
    result = TagResult()
    vocab_set = set(vocabulary)
    batches = [bookmarks[i : i + batch_size] for i in range(0, len(bookmarks), batch_size)]
    for n, batch in enumerate(batches, 1):
        if progress:
            progress(n, len(batches))
        pending = batch
        for attempt in range(2):
            system, user = build_prompt(pending, vocabulary, curated=curated, max_tags=max_tags)
            result.calls += 1
            reply = generate(system, user, min(6000, 200 + 40 * len(pending)))
            try:
                obj = parse_ai_json(reply)
                rows = obj.get("tags") if isinstance(obj, dict) else obj
                if not isinstance(rows, list):
                    raise AIResponseError('the reply has no "tags" list')
            except AIResponseError as exc:
                if attempt == 1:
                    result.errors.append(f"batch {n}: {exc}")
                continue
            allowed = {b.id for b in pending}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                bid = str(row.get("id", "")).strip()
                if bid not in allowed:
                    if bid:
                        result.invented.append(bid)
                    continue
                raw = row.get("tags")
                if isinstance(raw, list):
                    result.tags[bid] = clean_suggestions(raw, vocabulary=vocab_set, curated=curated, max_tags=max_tags)
            pending = [b for b in pending if b.id not in result.tags]
            if not pending:
                break
    result.missing = [b.id for b in bookmarks if b.id not in result.tags]
    return result
