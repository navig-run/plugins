# Changelog

## 0.2.0 — 2026-09-28

### Added
- **Runs on its own.** `pip install navig-ebay` now gives a `navig-ebay` command with no navig
  installed: the same app navig mounts as `navig ebay`. It no longer depends on navig; eBay
  credentials and OAuth tokens stay in the encrypted vault through the standalone
  `navig-vault` package, which is the very vault navig reads when installed.

### Fixed
- **Settings were saved through the wrong path, and could be erased.** The navig branch of the
  config writer called `atomic_write_yaml(path, data)` with its arguments reversed, so it always
  failed into a plain fallback. And a config file that could not be read loaded as defaults,
  which the next save wrote over the real settings. Reading now refuses an unreadable or corrupt
  file instead, so nothing is overwritten.
- **Standalone and navig used different config folders.** Without navig the config went to
  `~/.navig/config/navig-ebay/`, while navig uses `~/.navig/navig-ebay/`; both now use navig's.

All notable changes to `navig-ebay` are documented here.

## [0.1.0] — unreleased

Initial release. First-party navig plugin for selling on eBay via the official
Sell APIs, mounted as `navig ebay`.

### Added
- **Auth**: `creds set`, `auth login` (RuName consent → vault), `auth status`,
  `auth logout`; OAuth user-token exchange + auto-refresh; client-credentials app
  token for the Browse API. Sandbox and production environments.
- **Setup**: `env`, `location add`, `policies sync`, `doctor` (gates publish on
  creds · token · location · policies prerequisites).
- **Listing**: `draft` (upsert inventory item + create offer), `publish`, `list`,
  `show`, `edit`, `end`, `relist`, `bulk` (whole folder).
- **Pricing**: `price` via the Browse API — active asking prices (low/median/high),
  explicitly labelled "not sold".
- **Orders**: `orders`, `ship`; `offers` (best-offer eligibility).
- **Item YAML schema** with validation and round-trip mapping to eBay payloads.
- **EPS image upload**: local photo paths are uploaded to eBay Picture Service and
  substituted with hosted URLs; https URLs pass through.
- Bundled `ebay-ops` capability skill.
