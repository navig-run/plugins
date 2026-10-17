# Changelog — navig-bookmarks

## 0.1.0 — Unreleased

### Added
- **The bookmark manager, as its own plugin.** Replaces core's `navig links` (which now
  forwards here and says so). Runs as `navig bookmarks` inside navig and as
  `navig-bookmarks` with no navig installed. `navig bookmarks <url>` saves a bookmark.
- **Import with folders** from Netscape HTML, JSON, Chrome/Edge, Firefox (read from a
  private copy, so an open Firefox is fine; its tags become tags) and Safari. **Export** to
  Netscape HTML with nested folders, or JSON.
- **`organize`** — an AI folder plan using your own AI (navig's, or any provider key, an
  OpenAI-compatible URL, or a local Ollama). Dry run by default; the plan is rejected if any
  bookmark is missing, any id is invented or a folder is deeper than 3 levels; a plan that
  moves more than 20 % of the bookmarks or makes more than 1 000 changes needs `--force`.
  `--apply` writes an undo file; `--undo` restores it.
- **`tag --ai`** — durable, searchable tags that reuse your vocabulary (`--curated`: only
  existing tags). Tags remember where they came from (you, AI, rule); an AI re-run replaces
  only AI tags.
- **`check`** — dead links and redirects from headers only, every redirect hop checked
  against the SSRF guard, with a Wayback Machine copy suggested for dead links.
- **`dedupe`** — finds the same page saved twice (tracking parameters, `www.`, http/https and
  query order ignored); `--apply` merges tags, notes and visits.
- **`rules`** — offline `domain/path -> tags` rules applied on add and import.
- **Automatic, one-way copy of `navig links` data** (`links.db`), read-only and safe to
  repeat; a bookmark deleted here is never copied back.
