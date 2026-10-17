# The built-in `github` plugin

A **GitHub command palette** for the project you're in — pull requests, issues, Actions/CI, releases,
and repo/auth views — driven by the [`gh`](https://cli.github.com) CLI. It auto-activates in any repo
whose remote points at GitHub, shows an `owner/repo` banner, and (once refreshed) the repo's stars,
open-PR count, and latest CI status.

It carries **zero** project-specific knowledge and stores **no** credentials: `gh` owns its own auth,
and every action runs as an argv array (no shell).

## Activation

The plugin turns on automatically (toggle it in the Plugins manager, key `p`) when the project's
`.git/config` has a **GitHub** remote — `github.com` **or** a GitHub Enterprise host, in any of git's
URL forms (`https://`, `ssh://`, or `git@host:owner/repo`). The `origin` remote wins; otherwise the
first GitHub remote is used.

> Detection reads `.git/config` directly (the `detect`/`contribute` phases can't spawn a process). A
> linked **worktree** or **submodule** whose `.git` is a *file* (not a directory) isn't detected —
> run the menu from the main checkout.

## The palette

A **GitHub** rail. List views drill down with a picker (like the `ports`/`wrangler` plugins):

| Action | What it does | Risk |
| --- | --- | --- |
| **Status** | `gh pr status` — your PRs + the current-branch PR | safe |
| **Pull requests…** | list open/all → pick → view · open in browser · **check out** (confirmed) · or create a new PR | safe |
| **Issues…** | list open/all → pick → view · open in browser · or create | safe |
| **Actions / CI…** | recent runs → pick → summary · full log · watch live · open · **re-run failed** (confirmed) | safe |
| **Releases…** | list · **create** a release (tag + generated notes — publishes, confirmed) | confirm |
| **Repo overview** | `gh repo view` | safe |
| **Open on GitHub** | `gh browse` — the repo in your browser | safe |
| **Refresh stats** | fetch stars / open-PR count / latest CI → the banner cache | safe |
| **Auth status** | `gh auth status` — which account `gh` uses | safe |

### Banner

`owner/repo` shows immediately (read offline from `.git/config`). After **Refresh stats** the banner
adds the live numbers, e.g.:

```
GitHub · navig-run/labs · ★128 · 3 PRs · CI ✓
```

CI glyphs: `✓` success · `✗` failure/cancelled/timed-out · `…` in-progress/queued. The numbers come
only from the plugin's own cache — the banner never makes a network call.

## Safety

- Every action runs `gh` as an **argv array — no shell**, so a PR number, tag, or title can't be
  reinterpreted as a shell token.
- The two actions with side effects confirm first: **PR check out** (switches your working branch) and
  **create a release** (publishes to GitHub). **Re-run failed** also confirms.
- No credentials pass through the plugin — `gh` handles auth. If `gh` isn't installed, actions report
  a clear "install it from cli.github.com" message instead of failing obscurely.

## Settings

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `banner` | toggle | on | show the `GitHub · owner/repo …` stat line |

## Relationship to `navig github`

This is the **menu-surface** GitHub pack (day-to-day PRs/issues/CI in the project menu). It is
separate from the standalone **`navig-github`** Python plugin (repo backup / mirror / export). They
don't overlap.

## Internals

```
src/plugins/builtin/github/
  index.ts            plugin def — detect · contribute · handlers
  config.ts           loadGithubRepo(root) — read .git/config → { owner, repo, host }
  banner.ts           owner/repo (+ cached stars / PRs / CI) stat line
  handlers.ts         interactive handlers (the only side effects: spawn gh, prompt, cache)
  engine/
    remote.ts         parse git remote URLs + .git/config (pure)
    command.ts        pure argv builders for every gh op (unit-tested)
```

Tests: `test/github.test.ts` (remote URL parsing across all forms, `.git/config` parsing + origin
preference, argv builders, banner with/without cache, and end-to-end detect/contribute over a temp
repo).
