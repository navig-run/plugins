# Paperwork — `navig paperwork` (part of navig-cabinet)

File business paperwork into a navig space — invoices, quotes, contracts, tax notices —
by reading the documents rather than trusting their filenames.

```bash
navig paperwork scan H:\docs C:\Users\me\Downloads   # → a reviewable plan
navig paperwork review --needs-review                 # what still needs you
navig paperwork apply --dry-run                       # rehearse
navig paperwork apply --yes                           # copy, verify, quarantine
navig paperwork undo                                  # put it all back
navig paperwork index                                 # rebuild the index from disk
```

## What it is for

An archive of business paperwork accumulates in the places documents land — a downloads
folder, an external drive, a messaging app's export — and a folder called
`Factures-Invoices` ends up holding both the invoices you *issued* and the ones you
*received*. Those two are opposite entries in your books, and only the content of the
document separates them.

This plugin reads each document, decides what it is, gives it a canonical name and a
place, and moves it there without ever losing a file.

## How it decides

**Ownership first.** Before anything is scored, a veto asks whether the document is the
business's at all. This runs first because the documents that most need catching are the
ones that score well: a dental fee note named `facture-….pdf`, sitting in a folder full
of invoices, is a medical record. Vetoed documents are written to a **handoff manifest**
naming the space or tool that should own them — and are never moved, copied or modified.

The veto keys on document *type*, never on the presence of personal data. A sole
trader's social-security number appears on their URSSAF attestations, their payslips and
their company filings; "contains a NIR" carries no signal, while "is a prescription"
does.

**Then scoring.** Additive weights per class, with a filename outweighing incidental
body vocabulary — a contract discusses "facturation", a quote quotes tax articles.
A single unambiguous signal can carry a document alone, which matters because scanned
invoices routinely extract to zero text.

Anything ambiguous lands in `review` rather than being guessed at.

## Safety model

Mirrors `navig-explore`'s proven copy-verify-trash: a streaming copy that hashes the
source in the same read pass, verification by re-hashing the written file, `os.replace`
into place, and only then the source moved to a quarantine inside the space.

- **Nothing is ever hard-deleted.** Sources go to `.navig/paperwork/.trash/`.
- **A failed verification keeps the source.** A bad copy never costs the original.
- **A destination that exists with different content is never overwritten** — both files
  are left in place and the run reports a collision.
- **Idempotent and resumable.** A destination that already matches by hash is a skip, so
  an interrupted run resumes and a rerun is free.
- **`undo` refuses a destination edited since filing**, because deleting it would
  destroy work that exists nowhere else.
- **Every write is inside the target space.** A destination that escapes it is refused.

## Requirements

`pypdf` and `PyMuPDF` for PDFs, `python-docx` for Word files, and `tesseract` on PATH for
scanned pages. `scan` refuses to run without a document reader rather than producing a
confident-looking plan built from filenames alone.

Extraction is **local by default**. `--cloud-ocr` is an explicit opt-in, because this
tool reads tax, bank and medical documents.

## The plan file

`scan` writes `<space>/.navig/paperwork/plan.csv` and touches nothing else. `apply` reads
only that file. In between it is an ordinary spreadsheet: correct a `decision`, a
`doc_class` or a `dest_rel`, save, and re-run. That is the review gate.

| Column | |
|---|---|
| `decision` | `migrate` · `review` · `handoff` · `trash-dupe` · `skip` — **editable** |
| `doc_class` | the document type — **editable** |
| `dest_rel` | destination inside the space — **editable** |
| `confidence` | 0.00–0.99; ≥0.80 migrates, below that waits for you |
| `signals` | why it landed there — the audit trail |
| `dup_group` / `near_group` | exact and suspected duplicates |

## Duplicates

Exact matches (SHA-256) collapse automatically to one deterministically-elected keeper.
Near-duplicates — same name, different content — are grouped, flagged and left for a
person. There is no fuzzy matching: at archive scale no similarity threshold is
defensible, and the failure mode is silently discarding a real document.
