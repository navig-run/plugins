# Changelog — navig-browser


## 0.2.1 — 2026-10-14

### Fixed
- **`record_stepped` imported the media engine from its pre-extraction path.** #1908 moved
  `video_edit` to `navig-media-core` and repointed `record()`, but missed this newer
  function, so every stepped stage render died with `No module named
  'navig_generate.media.video_edit'` — the render path the Reel Stage depends on.
## Unreleased

### Fixed
- **`navig cdp launch` silently failed for Electron apps started from a VS Code terminal.**
  Such a shell exports `ELECTRON_RUN_AS_NODE=1` (and `VSCODE_*` IPC variables); a `Code.exe`,
  `Discord.exe` or `Notion.exe` launched with it runs as plain Node, rejects every flag
  (`bad option: --user-data-dir=…`), never opens its debug port, and the launch just timed out
  with no hint why. `launch_with_cdp` now hands the app a scrubbed environment (`VSCODE_PORTABLE`,
  which is set on purpose, is kept). Verified against a real VS Code from a polluted shell:
  before — no CDP target, after — up.

### Changed
- **`record`'s mp4 encode comes from `navig-media-core`** (the ffmpeg primitives extracted
  from navig-generate). The dependency was repointed accordingly — same code, same behaviour.

### Added
- **auto-login refuses to form-fill an SSO-linked credential.** A login captured by
  `navig vault login capture` holds no password; when its session no longer restores,
  `navig cdp login` now returns `session_expired` naming the re-capture command instead
  of submitting an empty password field. When the record carries a linked API credential
  (`api_ref`), the message also says so — the cookie session was never the only channel,
  so agent-side automation keeps working without the browser.
- **Lenses run in every browser the engine starts or attaches.** Each controller
  (`BrowserController`, `CDPBridge`, stealth, system Chrome, Firefox/Camoufox) registers the
  user's enabled Lenses (navig core's `navig lens`, `docs/lenses.md`) as init scripts, and
  every fresh CDP attach re-registers them. It is best-effort: standalone, against a core
  without Lenses, or with `NAVIG_LENSES=0` nothing is registered and the browser works as
  before.
- `navig cdp inject @file.user.js` honours the `==UserScript==` header (`@match`, `@run-at`,
  `@grant`, `@require`) through the Lens runtime, instead of running the file on every page.

### Fixed
- **`navig cdp new -e` / `launch -e` reported an extension as loaded when the browser loaded
  nothing.** Google Chrome 137+ accepts `--load-extension` and silently ignores it (measured
  on 151: `chrome://extensions` empty while the command printed `loaded_extension`). The
  browser is now asked what it installed (`chrome.developerPrivate` on chrome://extensions):
  a miss fails with exit 1 and a hint to use Chromium / Chrome for Testing, and `new` closes
  the throwaway session it opened; `launch` reports but never closes. When the browser can't
  answer, the result says "not confirmed" instead of "loaded".

## 0.1.0 — 2026-09-30

### Added
- **First release: navig's browser engine as its own package.** `navig/browser/` (launch,
  CDP bridge and actions, named profiles, stealth, hardened Chromium, Camoufox, proxies,
  autofill from the vault, recipes, templates) moved here from navig core, with
  `navig.browser.<module>` left as identity aliases. `navig cdp` moved with it and is also the
  `navig-browser` console script.
- Runs without navig: paths, settings, JSON and SSRF through navig-sdk; logins and sessions
  through navig-vault; `record`'s mp4 encode through navig-generate. `drive`, `stealth` and
  `firefox` extras.
- Launch records (`cdp-launched.json`), profiles (`cdp-profiles.json`) and the signer cache keep
  their paths in navig's config folder, and the leak rules are unchanged: `stop` only closes a
  recorded browser that is still the same process.
