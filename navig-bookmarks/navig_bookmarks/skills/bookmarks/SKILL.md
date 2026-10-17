---
name: bookmarks
description: Save, search, import, export, organize and clean up the user's browser bookmarks. Use when the user wants to bookmark a URL, find a saved link, import bookmarks from Chrome/Edge/Firefox/Safari or an HTML export, sort bookmarks into folders, tag them, find dead links or duplicates, or export bookmarks back to a browser.
activation_keywords:
  - bookmark
  - bookmarks
metadata:
  version: 1.0.0
  toolsAllowed:
    - bash_exec
---

# Bookmarks

Provided by the **navig-bookmarks** plugin: `navig bookmarks <command>` inside navig,
`navig-bookmarks <command>` on its own.

- Save: `navig bookmarks <url>` (or `add <url> --tags a,b --folder "Dev/Python"`).
- Find: `search <words>`, `list --tag x --folder y`, `show <id>`, `open <id>`.
- Import: `import <file>` (Netscape HTML, JSON, Chrome/Edge `Bookmarks`, Firefox
  `places.sqlite`, Safari `Bookmarks.plist`) or `import --source chrome` for the default profile.
- Organize: `organize` prints an AI folder plan and changes nothing; `organize --apply`
  writes it (a very large plan also needs `--force`); `organize --undo <file>` reverts.
- Tags: `tag <id> <tag>`; `tag --ai --untagged` (AI tags replace only earlier AI tags);
  `rules add github.com dev` for offline rules.
- Health: `check` (dead links and redirects), `dedupe` / `dedupe --apply`.
- Export: `export bookmarks.html` (any browser imports it, folders kept) or `--format json`.

Notes:
- Show the `organize` dry run to the user and get their go-ahead before `--apply`.
- `delete` and `dedupe --apply` remove bookmarks — confirm with the user first.
