---
name: telegram-exports
description: >-
  Organize Telegram *export folders* (ChatExport_YYYY-MM-DD from Telegram
  Desktop's "Export chat history") on disk. Use when the user has a pile of
  ChatExport_* / DataExport folders to sort, dedupe, or audit — NOT for live
  account actions (that's `navig telegram`, the MTProto manager). Triggers:
  "organize my telegram exports", "sort ChatExport folders", "dedupe telegram
  export media", "which chat do these files belong to".
---

# Telegram export-folder organizer

Offline. Reads/moves files under `--root`; never touches the network or the account.
The live counterpart is `navig telegram …` (MTProto) — use that for dialogs/history/move.

## Scheme

`<root>/<Category>/<Chat name>/<YYYY-MM-DD>/` — Category from chat `type`
(personal_chat→People, bot_chat→Bots, channel→Channels, supergroup/group→Groups).

## Workflow

1. **Triage first, then act.** Everything is dry-run/report by default; inspect output before
   passing `--apply` (organize/match) or `--confirm` (dedupe).
2. `navig telegram-exports organize --root <archive>` → review → `--apply` to file loose
   `ChatExport_*` folders.
3. `navig telegram-exports audit --root <archive>` — must report `Unresolved: 0`. Exit code 1
   means a logged move can't be resolved to a real location (investigate before proceeding).
4. `navig telegram-exports verify --root <archive>` — `Classifier drift: 0` means every filed
   chat still classifies into the folder it lives in.
5. `navig telegram-exports match --root <archive>` — pairs loose downloads (`_staging/by-type`)
   back to a source chat by original filename **and exact byte size** (strong) or name only
   (weak). `--apply` groups strong single-chat matches under `_staging/matched/<chat>/`.
6. `navig telegram-exports dedupe --root <archive>` — flags loose files that are byte-identical
   to a copy already inside their export (DUPLICATE = safe; ONLY-COPY = keep). `--confirm`
   re-hashes at delete time and removes only proven duplicates, logging to `_deletions-log.tsv`.

## Rules

- **Never delete without a verified surviving copy.** `dedupe` guarantees this; don't bypass it.
- Reversibility lives in `_reorg-log.tsv` (moves) and `_deletions-log.tsv` (deletes) — keep them.
- Sensitive exports (KYC, credential dumps) may be present; do not open, print, or exfiltrate
  contents — this tool only sorts folders, it never reads message bodies.
