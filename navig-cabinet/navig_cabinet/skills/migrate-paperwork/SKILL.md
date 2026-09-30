---
name: migrate-paperwork
description: Migrate scattered business paperwork (invoices, quotes, contracts, tax notices) from drives and download folders into a navig space, classified by document content rather than filename. Use when a space's finance folders are empty but paperwork exists elsewhere, when issued and received invoices are mixed in one folder, or when an archive needs deduplicating before filing.
---

# Migrate paperwork into a space

Four steps. Nothing moves until step three, and step four undoes it.

## 1. Scan

```bash
navig paperwork scan <source>... --space <space>
```

Reads every candidate document, classifies it, groups duplicates, and writes
`<space>/.navig/paperwork/plan.csv`. Writes nothing else and moves nothing.

Refuses to run without a PDF reader installed — a filename-only scan produces a
confident-looking plan that files medical bills under invoices.

Extraction is local by default. Only pass `--cloud-ocr` if the person has explicitly
agreed to send these documents to a paid provider; the corpus typically contains tax,
bank and health records.

## 2. Review

```bash
navig paperwork review --needs-review    # rows the classifier would not guess
navig paperwork review --handoff         # documents that are not the company's
navig paperwork review --dupes           # exact and suspected duplicates
```

The plan is a spreadsheet. Correcting `decision`, `doc_class` or `dest_rel` and saving
is the supported way to override any judgement — re-running `apply` honours it.

**Always show the handoff list to the person before applying.** It is where personal
identity, medical and credential files land, and they were deliberately excluded from
the space.

To act on it:

```bash
navig paperwork handoff --space <space> --dry-run   # what goes where
navig paperwork handoff --space <space> --yes       # file + encrypt
```

ID scans and medical records are **encrypted into the cabinet** (`navig cabinet`), never
filed as plain files; their originals stay in place until the person deletes them.
Benefits and housing letters go to the space that works with them. Credentials are never
moved (they belong in `navig vault`), and neither is anyone else's document.

## 3. Apply

```bash
navig paperwork apply --dry-run    # rehearse
navig paperwork apply --yes        # execute
```

Without `--yes` it prints counts and changes nothing. Each document is copied,
verified by SHA-256, then its source is moved to a quarantine inside the space.

Exit 1 means at least one verification failed or a destination collided — in both
cases the source was kept and nothing was lost. Read the reported rows; do not re-run
blindly.

## 4. Index

```bash
navig paperwork index --space <space>
```

Rebuilds the document index from disk and reports the next invoice number plus any
gaps in the issued series. Run it after applying, and any time files were moved by
hand.

## If something looks wrong

```bash
navig paperwork undo
```

Restores every source from the quarantine. Safe to re-run. It refuses to remove a
destination whose content changed since filing, because that edit exists nowhere else.

## What to tell the person afterwards

- How many documents moved, and how many are waiting in `review`.
- The handoff count: how many went into the encrypted cabinet, and which spaces the
  rest belong to — and that the plain originals of the encrypted ones are still where
  they were, for them to delete once they have checked `navig cabinet list --tag paperwork`.
- Any gap in the invoice series — for a French sole trader the sequence must be
  continuous and gapless, so a gap is a compliance question, not a cosmetic one.
- That the originals are in `.navig/paperwork/.trash/` until they clear them.
