# Changelog — navig-browser

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
