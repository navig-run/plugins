---
name: github-ops
description: Work with GitHub — back up, clone, search, or inspect repositories and account data. Use when the user wants to backup, clone, or search a GitHub repository, or list their repos, gists, or stars.
activation_keywords: [github]
# `depot` is also an English word (bus depot), so French uses phrases.
activation_keywords_ru: [гитхаб, "гит хаб", репозитор]
activation_keywords_fr: ["mes depots", "mon depot", "le depot", "ce depot", "depot git", "depot distant"]
metadata:
  version: 1.0.0
  toolsAllowed:
    - bash_exec
  envRequirements:
    - github_token
---

# GitHub Operations

This capability is provided by the **navig-github** plugin. Use `navig github
<command>` for all GitHub work — the group is authenticated automatically from
the vault `github_token`.

- Run `navig github --help` to see the full command surface (backup, clone,
  search, repos, and more).

Notes:
- Prefer `navig github` over raw `git` or the GitHub web UI for account-scoped
  operations (it handles auth + pagination).
- Back up before any destructive operation and confirm with the user first.
