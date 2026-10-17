# Changelog — navig-notes

## 0.1.0 — Unreleased

### Added
- **The notes store** (`notes.db`, schema v1): every note verbatim (unknown fields survive),
  searchable columns, an FTS5 index, a store-wide change counter for incremental pulls, and
  tombstones so deletions reach every copy.
- **`POST /api/deck/notes/sync`** — the NAVIG Dock's round trip: push edits and deletions, get
  back what changed here since the cursor. The higher revision wins; a stale writer is handed
  the stored copy. Plus list / search / status / get and `POST /api/deck/notes` for notes made
  by other surfaces. This computer only, behind the free `notes` module.
- **`navig notes`** — list, search, show, add, rm, export (JSON the Dock can import, or
  Markdown), status.
