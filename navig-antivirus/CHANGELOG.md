# Changelog

All notable changes to `navig-antivirus` are documented here.

## [0.1.1] — 2026-07-19

### Fixed
- `recover-profiles`: the "is the browser closed?" safety guard was hardcoded to check
  **Chrome** even when run against Edge/Brave (`--browser edge|brave`), so a write could land
  under a live browser and be overwritten (or corrupted) on its next flush. The guard now
  checks the selected browser's process.
- `recover-profiles`: `Local State` is now written **atomically** (sibling temp + `os.replace`,
  with temp cleanup on failure). A torn write no longer corrupts the profile index — the
  original stays byte-for-byte intact if the write is interrupted.

### Added
- First test suite (`tests/test_recover.py`): plan, dry-run, atomic rebuild + backup,
  browser-aware guard, and torn-write-leaves-original-intact.

## [0.1.0] — 2026-07-07

### Added
- Initial release. First-party navig plugin exposing `navig antivirus`.
- `extensions` — Chromium-family (Chrome/Edge/Brave) extension malware/PUP scanner: static
  analysis of each profile's `Secure Preferences`, risk scoring (proxy/traffic-MITM, all-sites
  cookies/webRequest, management, debugger, nativeMessaging, sideload/non-Web-Store, unsafe-eval,
  known-malware ids), dedupe-by-id across profiles, risk bands.
- `registry` — Windows registry audit of force-installed Chrome extensions and
  `ExtensionInstallForcelist`/`ExtensionInstallBlocklist` policies, flagging bundleware signatures
  (`clid=` affiliate `install_parameter`, insecure `http://` update URLs).
- `system` — on-demand Windows Defender scan (`quick`/`full`/`custom`) via `MpCmdRun.exe`, with
  honest Malwarebytes detection (no consumer scan CLI → Defender fallback).
- `recover-profiles` — Chrome profile-index recovery: rebuild `Local State → info_cache` from each
  profile's `Preferences` (dry-run default; `--apply` backs up + requires the browser closed).
- `report` — one-pass read-only health summary across all three surfaces.
