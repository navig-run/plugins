---
name: personal-cabinet
description: Store, find and retrieve a person's important files — ID and passport scans, medical records, insurance, contracts, diplomas, photos, recordings — in the encrypted navig cabinet. Use when someone wants to keep a document safe, find "my passport scan" or "the blood test from March", check what expires soon, get a copy out, or back their documents up.
---

# The personal cabinet

`navig cabinet` is an encrypted store for files a person must never lose. Use it for
documents, not secrets: an API key or a password goes in `navig vault`.

## Put things in

```bash
navig cabinet add <file-or-folder>... [--category identity|medical|insurance|finance|housing|legal|education|vehicle|photos|recordings|other]
                  [--tag x] [--expires YYYY-MM-DD] [--issuer "..."] [--title "..."]
```

- Originals are kept. Only pass `--move` if the person asked for the originals to be removed.
- Always pass `--expires` for passports, ID cards, residence permits, driving licences and insurance, when the date is known. It powers `expiring`.
- Text is read locally for search. Audio and video are transcribed only with `--transcribe`, which is slow; ask before using it on long recordings.

Business paperwork (invoices, quotes, contracts, tax) is NOT for the cabinet: file it with
`navig paperwork` (skill `migrate-paperwork`), which keeps it readable for the accountant
and encrypts any ID or medical document it finds into the cabinet by itself.

## Find things

```bash
navig cabinet search <words>      # titles, tags, notes AND the words inside the scans
navig cabinet list [--category medical] [--tag x] --json
navig cabinet show <id> --json
navig cabinet expiring --within 90 --json
```

## Get things out

```bash
navig cabinet open <id>                     # view in its usual app (temporary decrypted copy)
navig cabinet export <id>... -o <dir>       # plain copies with original names — tell the person they are NOT encrypted
navig cabinet close                         # remove leftover decrypted copies
```

## Safety rules

- Never paste a document's extracted text into a chat, log or file unless the person asked for that specific content. It is usually medical or identity data.
- `remove` goes to the trash; `remove --purge` is permanent. Confirm with the person before purging.
- If `status` says **Never backed up**, suggest `navig cabinet backup -o <file>` — with
  `--with-vault` it also saves the vault's secrets, which otherwise only open on this machine.
  - A machine-locked cabinet does not survive a reinstall.
  - The backup passphrase is the person's to choose. Never invent one, and never store it.
- If the cabinet is passphrase-locked, the person types the passphrase. Do not ask for it in chat.
