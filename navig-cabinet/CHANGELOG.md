# Changelog

## 0.3.0 — 2026-09-28

### Added
- **Runs on its own.** `pip install navig-cabinet` now gives a `navig-cabinet` command with no
  navig installed — `navig cabinet` and `navig paperwork` in one (`navig-cabinet paperwork …`).
  It no longer depends on navig; the encryption key stays in the shared vault through the
  standalone `navig-vault` package. Without navig, text files and Word documents are still read
  so the cabinet can be searched by what they say; PDFs, scans (OCR) and recordings are read by
  navig's extractor and, on their own, are stored and searchable by title and tags (the note
  says so). Hints name the command that exists where you are.

### Fixed
- Without navig, `recurrent.yaml` and a space's declared profile were silently ignored (their
  reader was imported from navig inside a broad `except`); they are read on their own now.
- `paperwork --space /absolute/path` needed navig's space registry even for a path; only a
  space NAME needs it now.

## Unreleased

- **`navig cabinet backup --with-vault`: one passphrase-sealed file for everything
  personal.** The vault's own storage only opens on the machine that made it and it has
  no export, so a dead disk lost every API key. The backup now carries every vault
  secret too. `restore` puts them back re-sealed under the new machine's key, keeping
  their ids and never overwriting a secret already there (`--skip-vault` to leave the
  vault alone). Unreadable vault items are named, not silently dropped.
- `restore` now exits 1 when any item or secret failed to restore (it reported the
  errors but exited 0).

## 0.2.0 — 2026-09-27

**`navig paperwork` is now part of this plugin.** One install gives both commands; the
separate `navig-paperwork` package (never published) is gone, and `navig[paperwork]` now
installs this plugin. Every `navig paperwork` verb, flag, file and path is unchanged.

- ID scans and medical records found by a paperwork scan are **encrypted into the
  cabinet** by `navig paperwork handoff` instead of being filed as plain files into a
  space. The document type decides, so a manifest written before this release cannot put
  a medical record back into a plaintext tree. Originals are left in place.
  `--to cabinet` selects just that half. Benefits and housing letters keep their space
  route.
- Paperwork's module now sits in a real catalog category (`tools`), not an unlabeled one.
- **Expiry reminders that arrive by themselves.** The navig daemon warns 90, 30 and 7
  days before a document expires, then once when it has expired. It sends each
  threshold once per document and date, through the notify router (deck + Telegram),
  daily after `cabinet.reminders.hour`. Messages name the category and id, never the
  title.
  - The daemon reads a machine-bound index of `(id, category, expires)`, so it never
    needs the passphrase.
  - A failed delivery is not marked sent and retries hourly.
  - `navig cabinet remind [--dry-run]` sends on demand.
  - `status` shows whether reminders are on.
- **Works with the navig on PyPI (3.25.0), as its floor says.** The few core helpers
  newer than that release (`resolve_user_path`, `coerce_int`, the Telegram notifier, the
  guarded LLM door) are imported through `navig_cabinet._core` with fallbacks. The pure
  helpers are copied exactly. On the older core, `paperwork --send` reports "not sent"
  and `paperwork reply` refuses with a clear message instead of raising ImportError.
  Verified by installing the built wheel next to PyPI's navig 3.25.0 in a clean venv and
  running add, search, expiring, remind, dates, backup, restore, export and verify.
- **Expiry dates read from the document itself.** On `add`, when no `--expires` is
  given, the cabinet reads the passport / ID-card machine-readable zone (ICAO check
  digit verified, so a misread is rejected) or a labelled expiry in FR/EN/DE/ES/IT. It
  never takes a bare date.
  - Detected dates are marked (`expires_detected`, shown as "read from the document").
  - `edit --expires` makes a date the operator's own.
  - `navig cabinet dates [--apply]` backfills documents already stored.
  - Items stored before the field existed load unchanged.
- **The Cabinet desktop app** (NAVIG OS → Apps → Security): search inside documents,
  category filter, expiring card, Add files, Open, and a passphrase unlock form.
  - Its `/api/deck/cabinet/*` routes answer only this computer: tunnel, Lighthouse and
    Mini App traffic gets a 403.
  - They return metadata only, never file content or OCR text.
  - Crypto, SQLite and OCR run off the gateway's event loop.

### Paperwork history (was navig-paperwork)

- The handoff manifest names the cabinet for ID and medical documents.
- 0.1.0: `navig paperwork scan | review | apply | undo | index`; content-based
  classification into eleven document classes with an ownership veto that runs before
  scoring; duplicate election; filename repair; copy-verify-quarantine execution with
  receipts, reversible via `undo`; local-only text extraction by default.

## 0.1.0 — 2026-09-26

First release.

- `navig cabinet add | list | search | show | open | close | export | edit | remove |
  undelete | expiring | backup | restore | import-paperwork | verify | status |
  passphrase set|clear`.
- Any file type and size: chunked AES-256-GCM streams (reorder/truncation/splice-proof),
  bounded memory for multi-GB videos, verified read-back before an item is cataloged.
- Everything descriptive is encrypted — title, filename, tags, expiry, notes and OCR text.
- Machine key by default (bound to the OS machine id); optional passphrase; a wrong
  passphrase never falls back to the machine key.
- Local-only text extraction (no cloud OCR, no plaintext cache); audio/video
  transcription opt-in via `--transcribe`.
- Portable passphrase backups (`.ncab`) restorable anywhere, plus a standalone
  recovery script that needs only `cryptography`.
- `import-paperwork` encrypts the ID/medical documents a `navig paperwork` scan set aside.
