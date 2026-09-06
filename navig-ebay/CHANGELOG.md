# Changelog

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
