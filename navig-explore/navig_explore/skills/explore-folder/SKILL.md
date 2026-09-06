---
name: explore-folder
description: Explore, organize, sort, or clean up a local folder of files and media — filter by type, preview, extract text, or delete. Use when the user wants to explore, organize, sort, tidy, or clean up a folder such as Downloads, a data dump, or a Telegram export.
metadata:
  version: 1.0.0
  toolsAllowed:
    - bash_exec
---

# Explore a Folder

This capability is provided by the **navig-explore** plugin. Point it at any
folder to walk the tree, bucket files by type, preview, extract text, and organize.

- `navig explore <folder>` — open the explorer for that folder (Downloads, a
  drive dump, a Telegram export…).

Run `navig explore --help` for options.

Notes:
- Non-destructive: edits live in a `.mediaexplorer/` sidecar and commits are
  reversible. Confirm with the user before deleting or committing changes.
