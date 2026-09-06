# Releasing navig-menu

This is the checklist to publish `navig-menu` to npm and cut a GitHub release. The package name is
**`navig-menu`** (unscoped, public).

## One-time setup

1. **Claim the name on npm.** `navig-menu` must be available (or already owned by you):
   ```bash
   npm view navig-menu   # "404 Not Found" = free to claim; anything else = taken
   ```
2. **Log in** to the account that will own the package:
   ```bash
   npm whoami            # confirm the right account
   npm login             # if not already logged in
   ```
3. **Add the npm token to GitHub** (only needed if you want CI to auto-publish on tag):
   - Create an *Automation* token at npmjs.com → Access Tokens.
   - Add it to the repo as the secret `NPM_TOKEN` (Settings → Secrets → Actions).
   - The token is used by [`.github/workflows/release.yml`](.github/workflows/release.yml).

## Every release

1. **Bump the version.** Keep these three in sync:
   - `version` in [`package.json`](package.json)
   - `TOOL_VERSION` in [`src/config/constants.ts`](src/config/constants.ts)
   - a new heading in [`CHANGELOG.md`](CHANGELOG.md)

   Or let npm do the package.json bump: `npm version patch|minor|major` (then update the other two).
2. **Verify locally** — the same gate `prepublishOnly` runs:
   ```bash
   npm run typecheck && npm test && npm run build
   ```
3. **Dry-run the publish** to see exactly what ships (should be `dist/`, `schema/`, `docs/`,
   `README.md`, `CHANGELOG.md`, `LICENSE` — see the `files` field):
   ```bash
   npm publish --dry-run
   ```

## Option A — publish manually

```bash
npm publish --access public
```

Then test it end-to-end from a clean shell:

```bash
npx navig-menu@latest --version
```

## Option B — let CI publish (recommended once `NPM_TOKEN` is set)

Tag the commit `vX.Y.Z` and push the tag. The `release` workflow runs typecheck → test → build →
cross-compiles the standalone binaries with Bun (5 targets) → publishes to npm with provenance →
attaches the binaries + `SHA256SUMS` to a GitHub Release.

```bash
git tag v1.0.0
git push origin v1.0.0
```

## Notes

- **Unscoped public package** — no `publishConfig` needed; `--access public` is harmless for
  unscoped names and required if you ever move to a `@scope/`.
- **`prepublishOnly`** already runs typecheck + test + build, so a broken build cannot be published.
- **Binaries** are produced by [`scripts/build-binaries.ts`](scripts/build-binaries.ts) via
  `bun run scripts/build-binaries.ts` (needs Bun; cross-compiling from Windows is flaky — the CI
  Linux runner handles all targets reliably).
- **Plugins** published by others should be named `navig-menu-plugin-<name>` (or
  `@scope/navig-menu-plugin-<name>`) so `navig-menu` auto-discovers them from dependencies.
