# Microsoft Store plugin — full reference

The built-in `store` plugin is a complete Microsoft Store (Partner Center) publishing pipeline
with a **native TypeScript engine** — token acquisition, app submissions, package upload, add-on
(IAP) lifecycle, price tiers, status, analytics, and live-listing pulls, all over the DevCenter
submission REST API (`manage.devcenter.microsoft.com/v1.0/my`). No PowerShell, no external CLIs;
it compiles into the single-binary build.

It is **project-agnostic by contract**: the plugin knows nothing about what your app is. One
`store.config.json` at the repo root describes one app or a whole catalog, and every behavior
that could be product-specific (category, certification notes, add-on naming, prices) comes from
that config — the plugin never injects a product default.

## `store.config.json`

```jsonc
{
  "version": 1,
  "credsFile": "scripts/.store-creds.json",   // optional fallback creds file — keep it gitignored
  "defaults": {                                // all optional
    "publishMode": "Manual",                   // "Manual" | "Immediate"
    "category": "Personalization_WallpaperAndLockScreens",
    "storeCut": 0.15,                          // fraction the Store keeps (analytics estimate)
    "notesForCertification": "…(${displayName} is substituted)"
  },
  "apps": [
    {
      "id": "aurora",                          // unique slug: pickers, cache keys, typed confirm
      "name": "Aurora",                        // display name
      "appId": "9NQXWX0MHS3B",                 // Store product ID — REQUIRED
      "identity": "AcmeSoft.Aurora",           // MSIX identity (derives the add-on productId)
      "package": "release/aurora/*.msixupload",// glob (wildcards in the file segment); newest wins
      "build": "store:release",                // optional — command that bumps+builds+packs a fresh
                                               //   package (bare word = npm script). Runs before submit
                                               //   when the package is missing, or on "Rebuild & submit".
      "metadata": "store/aurora/metadata.json",// optional → enables listing submission
      "screenshots": "store/aurora/screenshots",// optional dir of png/jpg
      "category": "…",                         // optional per-app override
      "notesForCertification": "…",            // optional per-app override (metadata.json wins)
      "addon": {                               // optional — omit for apps without an IAP
        "productId": "AuroraProUpgrade",       // default: identity last dot-segment + "ProUpgrade"
        "storeId": "9PXXXXXXXXXX",             // written back by the plugin after create/heal
        "priceUsd": 2.99,
        "title": "Aurora Pro Upgrade",         // default: "<name> Pro Upgrade"
        "description": "…"
      }
    }
  ]
}
```

With **one** app configured, pickers skip straight to it; with many, you pick (or run
"ALL published apps"). An invalid config produces an actionable validation error, never a crash.

### `metadata.json` fields

`displayName, description, shortDescription, features[≤20], keywords[≤7], notes.whatsNew
(→ releaseNotes), copyrightInfo, websiteUrl, privacyPolicyUrl, supportUrl, devStudio, shortTitle,
voiceTitle, applicationCategory, notesForCertification`. Fields that are absent are left
untouched on the submission. The IARC age rating is **not** settable via this API — it carries
from the cloned submission.

## Credentials

`AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET`, resolved first-non-empty-wins:
exported **env var** → a gitignored root **`.env`** (the preferred home — same three keys) → the
optional **`credsFile`** JSON (`{ "tenantId", "clientId", "clientSecret" }`). Only those three keys
are ever read from `.env`. The Azure AD app must be added in **Partner Center → User management →
Azure AD applications** with the **Manager** role.

Secret safety: values are resolved only when a handler runs, stay function-scoped, and never
reach caches, the banner, notify lines, or error text. *Check credentials* reports which NAMES
are set and can do a live token round-trip ("auth OK (source: env)").

## Actions

| Action | What it does |
|---|---|
| **Initialize store config** | *(shown when there's no `store.config.json` yet)* Scaffolds `store.config.json` + a `metadata.json` stub for this project — Tauri / .NET-MSIX build output auto-detected. Emits a clearly-marked `SET-STORE-PRODUCT-ID` placeholder for the one value it can't know (the Partner Center product ID). One action to make a brand-new app publishable. |
| **Publish / submit** | Pick app → mode: **Dry run** (create/reuse submission, apply listing + screenshots, upload package — never commits), **Submit (manual go-live)**, **Package only** (updates only), **Go live (Immediate)** — gated by a *typed* confirmation of the app id — or **Rebuild & submit** (shown when the app has a `build`: runs it — bump + compile + package — then submits under the manual gate). If no package matches and a `build` is set, it builds automatically first. Polls the commit until accepted. |
| **Generate listing (AI)** | AI-drafts description/features/keywords/what's-new → `metadata.json` (provider key → local `claude` CLI → paste-ready prompt file). Review before publishing. |
| **Certify (WACK)** | Runs the Windows App Certification Kit (`appcert.exe`) on the built `.msix`/`.msixbundle` next to the configured `package` — the same cert pass the Store runs server-side, so failures are caught locally. Windows + Windows SDK + an elevated shell. |
| **Submission status** | Live state per app (published / in certification / failed + cert-report URL) → status cache. |
| **Prices audit** | Local `addon.priceUsd` ↔ live tier, decoded both ways. Statuses: `OK`, `MISMATCH`, `LIVE & priced (opaque)`, `no base price yet`, `no add-on yet`, `free`. Read-only. |
| **Ensure Pro add-on** | Resolve or create the durable IAP, price it from the **app's** pricing model, submit. Writes a new Store ID back into `store.config.json` (with consent). |
| **Pull live listing** | Store listing → local `metadata.json`; fill-empty merge (local wins) or force overwrite. Screenshot bytes aren't downloadable — filenames/count only. |
| **Open Partner Center** | Opens the app's submissions page in the browser (no auth). |
| **Refresh stats** | Acquisitions + in-app acquisitions → downloads / sales / est-net per app → cache (feeds the banner). Partial failures keep last-known-good values. |
| **Check credentials** | Names-only creds report (env / `.env` / `credsFile`) + optional live token test. |

Settings: `stat` (banner line), `dryRunDefault` (preselect Dry run in the publish picker),
`pollTimeout` (15m/30m/60m).

### What the plugin does *not* build

The plugin does not itself compile or sign the MSIX — that step is toolchain-specific (Tauri
`tauri build`, or `makeappx` for a .NET/MSIX app). But it **can drive** the app's build: point
`apps[].build` at one command that bump-version + compiles + packs (e.g. an npm script
`store:release` = `bump-version && tauri:build:store`). Then **Rebuild & submit** and the
build-when-missing path run it before submitting — the legacy "ship a new version" flow, one action.
Version-bump policy stays inside that command; the plugin never edits versions itself. The STORE rail
also auto-claims any `store:*` script and lists it alongside the pipeline actions. Screenshot *capture* and logo/tile *asset
generation* are likewise per-app; the plugin validates screenshots (and skips oversized ones with an
ImageMagick hint) but never resizes them.

## Behaviors worth knowing (inherited from a battle-tested pipeline)

- **Pending-draft reuse** — a non-active pending submission is *reused*, not deleted, so manual
  Partner Center work (IARC, data declaration, add-on base price) survives re-runs. Only an
  actively-processing submission blocks (or is replaced with explicit consent). A stale upload
  SAS (`se=` within 5 minutes) forces a fresh submission.
- **Package/screenshot swap** — committed entries (with an id) become `PendingDelete`;
  uncommitted ones (no id, from an earlier never-committed run) are dropped, since the API
  rejects existing entries without an id. Logos are untouched. Replacing screenshots when the
  live listing has more than your local folder asks first.
- **First publish** — pricing is forced Free + NoFreeTrial; a listing is required (actionable
  error otherwise); `applicationCategory` must come from config/metadata; device families are
  initialized Desktop-only (never `Team`). A `CommitFailed` on a first publish maps to the
  human-gated checklist (privacy policy URL + IARC questionnaire) with a Partner Center deep
  link — the submission is left intact and a re-run commits cleanly.
- **Add-ons** — the pricing scheme (`isAdvancedPricingModel`) and market availability are read
  from the **published app submission** (a fresh add-on submission misreports them). Advanced
  tiers: Tier1012 = $0.99 stepping $0.10 ($2.99 = Tier1032); legacy: Tier2 = $0.99 stepping $1.
  A brand-new add-on's base price/currency must be set **once** in Partner Center — the
  `InvalidState` commit failure is detected and turned into that checklist.
- **Retry/throttle** — 429 honours the server's Retry-After (header or "Try again in N second"
  body); 408/5xx back off exponentially (8→128 s, capped). The add-on commit re-checks the
  submission status after a gateway error, because a 5xx commit may have landed.
- **Screenshots** — validated (min 1366×768, max 3840×2160, hard 50 MB, advisory 10 MB) via a
  pure PNG/JPEG header probe. The engine does **not** resize; oversized files are skipped with
  an ImageMagick one-liner suggestion.
- **Partner Center's new UI** may show "Pricing: Not started" for values set through this legacy
  API — the submission still certifies; don't re-enter them.

## Integration smoke runbook (before trusting go-live)

All read-only first, then a deletable dry-run. Each step runs from the interactive menu or
headlessly (`navig-menu run <action>` — handy for CI status checks):

1. **Check credentials** (`run store.creds`) → expect "auth OK".
2. **Submission status** (`run store.status`) → states match Partner Center.
3. **Prices audit** (`run store.prices`) → statuses look sane (no unexpected MISMATCH).
4. **Publish → Dry run** on one already-live app → inspect the prepared draft in Partner Center
   (listing, package, category, notes) → delete the draft there.
5. Only after 1–4 pass, use *Submit (manual go-live)* for a real update; keep *Go live* for when
   you trust the whole chain.
