# Changelog — navig-blackbox

## 0.1.1 — 2026-09-03

### Fixed
- **`nbb --help` crashed on a legacy Windows console.** The Typer help contains emoji;
  on a console using an ANSI/OEM code page (cp1251, cp866, cp437) rich cannot encode them and
  click raises `UnicodeEncodeError` while rendering `--help`, before a single line reaches the
  user. Measured against the published 0.1.0 wheel on cp1251: it tracebacked, and the same
  command under `PYTHONUTF8=1` worked. Inside navig this never happens because navig sets the
  console up — standalone nothing does, which is exactly the environment this package promises
  to work in. The CLI now reconfigures its own stdio before the app renders anything.

All notable changes to this package are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Fixed
- **A blocked `record` told the operator to run a command that does not exist.** When the
  blackbox is sealed, `navig blackbox record` refuses the append and printed "run `navig
  blackbox unseal` first". There is no `unseal` verb — the real form is
  `navig blackbox seal --unseal`. Wrong advice on a blocked path, i.e. exactly when the
  operator has to act on it.

## 0.1.0 — 2026-07-25

Initial release. The blackbox engine was extracted out of navig-core (`navig/blackbox/`)
into this standalone-first package — the `voice/`→navig-audio pattern.
`navig-core/navig/blackbox/*` are now thin re-export shims, so the two never fork and
`navig blackbox` keeps working unchanged.

### Added
- **Standalone-first package** — `pip install navig-blackbox` works without navig
  (`navig-blackbox` / `nbb` / `python -m navig_blackbox`), and as a library
  (`from navig_blackbox import get_recorder, install_crash_handler`).
- **`navig_blackbox._compat`** — one seam for every navig-internal touchpoint (console,
  atomic writes, paths, version, vault encryption); delegates to navig when present, falls
  back cleanly otherwise. Encrypted `.navbox` export degrades to plaintext (with a warning)
  standalone — data is never lost.
- **CLI** — `status`, `events`, `record`, `bundle`, `inspect`, `crashes`, `seal`, `unseal`.
- **Free `blackbox` module** registered for the Store / deck / os surfaces.

### Fixed
- **Standalone data-dir now matches navig's.** The `_compat` fallbacks resolved
  `~/.navig/blackbox` (and `~/.navig/logs`), but navig's real dirs are
  `~/.navig/data/blackbox` and the OS log dir (`%LOCALAPPDATA%/navig/logs` on Windows).
  The fallbacks now mirror navig exactly (honoring `NAVIG_DATA_DIR` / `NAVIG_CONFIG_DIR` /
  `NAVIG_LOG_DIR`), so events recorded standalone are read by a later `pip install navig`.
- Added `navig_blackbox.__version__`.

### Notes
- The recorder JSONL, crash dir, and `.navbox` bundle format are unchanged, so existing
  data and archives keep working.
