# Changelog — navig-vault

## 0.11.0 — 2026-10-16

### Added
- **Authority rate limits.** 120 requests/min per device, 20 failed signatures/min per claimed
  device, 10 enrollments/min per source (`Authority.handle(..., source=)`); 429 + `retry_after`,
  and the client keeps its cache.

### Fixed
- **Unreadable items are named, not fatal** (moved here: 0.10.0 was published before it).
  `Vault.unreadable_items()`; `write_backup` leaves them out (`skipped_unreadable`) so the backup
  stays restorable; `rotate()` refuses by label.

## 0.10.0 — 2026-10-15

### Added
- **Keyslots, recovery, backups, federation.** A random Vault Master Key wrapped per
  keyslot (`dpapi` via ctypes, `keyfile`, the legacy `fingerprint`, `passphrase`, and an
  X25519 `recovery` slot whose private key is shown once) — `keyslots.py`. Legacy vaults keep
  working until `Vault.migrate_to_keyslots()`, which re-encrypts nothing. `rotate()` is now one
  transaction (every DEK, history DEK, slot and the canary) and aborts on any unopenable item
  instead of skipping it. `put(expected_version=)` is compare-and-swap with sealed history
  (`VaultConflict`); audit rows carry the actor (`acting_as()`), deletes are audited. An
  expired passphrase session raises `VaultLocked` instead of falling back to the fingerprint
  key. `backup.py`: recipient-key backups + verified restore into an empty dir. `refs.py`:
  the `vault://<path>[#field][?v=N]` grammar. `federation/`: the vault authority (devices,
  grants, per-request HMAC signing, values sealed to the device) and its client (offline
  cache only per grant TTL).

## 0.9.0 — 2026-10-15

### Added
- **SSO-linked website logins (`navig vault login capture`).** `WebLogin` gained
  `auth_method` / `idp` / `idp_subject` / `session_ref` — legacy payloads default to
  `password`, so existing records are untouched — plus `add_sso_login()` (refuses to
  replace a stored password with an empty SSO record, and keeps the account label
  identical to the session label so autofill's restore always finds the session) and
  `infer_idp()` (best-effort IdP hint from captured cookie hosts/names — never an auth
  decision). `auth_method` and `idp` live in the plaintext metadata, so listing logins
  still never decrypts a payload.
- **`api_ref` — a login links the stored API credential for its site.** `detect_api_ref()`
  matches the host's brand prefix against vault providers with an enabled credential
  (`notion.so` → `notion`) and returns the provider id — never a token — which the login
  record carries (payload + plaintext metadata) so automation can outlive the cookie
  session. Legacy payloads read `None`; a miss is the normal case and costs an absent
  hint.

### Changed
- **Reading a secret no longer re-derives the master key every time.** In machine-fingerprint
  mode (no passphrase) the master key is a deterministic function of the vault salt and the
  machine identity, yet every read ran Argon2id again — about 350 ms each. Listing AI
  connections reads one secret per configured provider, so every NAVIG agent turn paid 1–2 s
  here before the model was even called. The derived key is now kept for the life of the
  process, keyed by (salt, machine identity), so a rotated salt, another vault, or a changed
  machine derives afresh. Passphrase-derived keys are not cached: their lifetime stays the
  explicit, TTL-bound unlock session. Measured: listing connections 804 ms → 23 ms.

## 0.8.0 — 2026-10-10

### Added
- **Export to SVG: back up the vault, or hand someone a password.**
  - `nv export --svg <labels…> | --all [--each]` seals secrets into an ordinary SVG picture:
    AES-256-GCM, a PBKDF2-SHA256 (600k) key, and the info block bound in as AAD.
  - The passphrase is generated (~125 bits, shown once) unless you choose one (14+
    characters, via prompt or `NAVIG_VAULT_SVG_PASSPHRASE`, never argv).
  - `--each` gives every secret its own file and passphrase.
  - File names are random and the file reveals no labels.
  - `nv open` shows what an export holds (masked; `--reveal`). `nv import` adds it to this
    vault and never overwrites a secret already there.
  - `nv opener` writes `open-vault-svg.html`, which opens an export in any browser, offline,
    with WebCrypto.
  - This is the dormant *SVG Vault* project's technique, now always encrypted. Its old plain
    files still open (`nv open old.svg --save <folder>`).
  - New modules: `navig_vault.svg` (format), `transfer` (vault ↔ entries; the same shape
    navig-cabinet's backups carry), `svg_export` (what both CLIs call).
  - `svg_export.seal_texts()` is the same export held in memory, for a caller that saves the
    file itself. The NAVIG OS Vault app's Export button uses it, so the daemon writes no file.

### Fixed
- **A person whose account is named `navig` is no longer treated as a system install.**
  Only a system account (uid < 1000, or a home outside /home) or an explicit
  `NAVIG_SYSTEM_SERVICE=1` means /etc/navig — the same rule as navig core, which this
  mirrors.

## 0.1.0 — 2026-09-01

First changelog entry. This package existed before this file did, so earlier
history lives in the monorepo's git log rather than being reconstructed here —
an invented history would be worse than an honest starting point.

### Packaging
- Declares its dependency on `navig` (or deliberately does not, if it runs
  standalone) and ships the full licence text in the wheel.
