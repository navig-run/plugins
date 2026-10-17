# Changelog — navig-chats

## 0.1.0 — Unreleased

### Added
- **Sharing levels** (`share`, `shared`) — `off` / `local` / `agent` per folder (inherited) or per
  chat; the most private folder wins. Schema v2 (additive migration).
- **`handoff`** — Continue this work: a cited context pack (decisions current/superseded, open
  items, proposals, code, references, gaps), to a file or a space's `.navig/plans/briefs/`.
- **Deterministic extraction** (`extract.py`) of decisions, tasks, questions, snippets and links
  from explicit markers (EN/FR/RU) and bookmarks — shared by the handoff, the Library and Recall.
- Routes: `GET /brain`, `POST /handoff`, `GET /{id}/extract`; the overview lists ChatGPT Projects.
- **The chat library, as its own plugin.** `navig chats` inside navig, `navig-chats` on its
  own. One SQLite file (`<navig data dir>/chats.db`), separate from the agent's memory.
- **`import`** — ChatGPT's account export (the zip, its folder, `conversations.json` or the
  numbered split files). Streams the file, keeps every branch with the shown one marked,
  records attachments as metadata only. Re-running it changes nothing; a broken chat is
  reported and the rest still import.
- **`search`** — every message and title, BM25-ranked, with `"phrases"`, `-exclusions`,
  `prefix*` and `OR`; `--role`, `--code`, `--folder`.
- **Folders, tags, pins, local renames** — a local overlay that never changes ChatGPT.
- **`export`** — Markdown with front matter, text, self-contained HTML, a JSON bundle, or a
  folder-aware zip with a sha256 manifest; **`restore`** brings a NAVIG archive back into the
  library.
- **`/api/deck/chats/*`** — for the NAVIG Dock's ChatGPT Lens and Chat Library page. Answers
  only this computer.
