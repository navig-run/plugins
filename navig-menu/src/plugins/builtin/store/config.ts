/**
 * `store.config.json` — the project-agnostic contract that drives the Microsoft Store plugin.
 * Any MSIX project (one app or many) describes its publishable apps here; the plugin carries
 * ZERO product knowledge of its own. See docs/store-plugin.md for the full reference.
 */

import { readFileSync, writeFileSync, readdirSync, statSync, existsSync } from "node:fs";
import { join, dirname, basename, isAbsolute } from "node:path";
import { z } from "zod";

export const StoreAddonSchema = z.object({
  /** Partner Center productId. Default: identity's last dot-segment + "ProUpgrade". */
  productId: z.string().optional(),
  /** Store ID of the in-app product — written back by the plugin after create/heal. */
  storeId: z.string().optional(),
  priceUsd: z.number().positive(),
  title: z.string().optional(),
  description: z.string().optional(),
});

export const StoreAppSchema = z.object({
  /** Unique slug: pickers, cache keys, and the typed go-live confirmation. */
  id: z.string().min(1),
  /** Display name. */
  name: z.string().min(1),
  /** The app's Store product ID (e.g. 9NQXWX0MHS3B). */
  appId: z.string().min(1),
  /** MSIX package identity (used to derive the add-on productId). */
  identity: z.string().optional(),
  /** Repo-relative glob for the upload package (.msixupload/.msixbundle/.appxupload); newest wins. */
  package: z.string().min(1),
  /**
   * Command that produces a fresh upload package — ideally version-bump + compile + pack in one
   * (e.g. an npm script `store:release`). Run automatically before submit when no package matches,
   * or on demand via the "Rebuild & submit" publish mode. A bare word is an npm script
   * (`<pm> run <word>`); anything with a space runs verbatim as argv. Version-bump policy lives in
   * this command — the plugin never edits versions itself (too toolchain-specific).
   */
  build: z.string().optional(),
  /** Repo-relative path to a listing metadata.json — enables listing submission. */
  metadata: z.string().optional(),
  /** Repo-relative dir of listing screenshots (png/jpg). */
  screenshots: z.string().optional(),
  category: z.string().optional(),
  notesForCertification: z.string().optional(),
  addon: StoreAddonSchema.optional(),
});

export const StoreConfigSchema = z.object({
  version: z.literal(1),
  /** Repo-relative creds file ({tenantId, clientId, clientSecret}); env AZURE_* always wins. */
  credsFile: z.string().optional(),
  defaults: z
    .object({
      publishMode: z.enum(["Manual", "Immediate"]).optional(),
      category: z.string().optional(),
      storeCut: z.number().min(0).max(1).optional(),
      notesForCertification: z.string().optional(),
    })
    .optional(),
  apps: z.array(StoreAppSchema),
});

export type StoreConfig = z.infer<typeof StoreConfigSchema>;
export type StoreApp = z.infer<typeof StoreAppSchema>;

export const STORE_CONFIG_FILE = "store.config.json";

export type LoadConfigResult = { ok: true; config: StoreConfig } | { ok: false; error: string } | { ok: false; missing: true; error: string };

export function loadStoreConfig(root: string): LoadConfigResult {
  const path = join(root, STORE_CONFIG_FILE);
  if (!existsSync(path)) {
    return { ok: false, missing: true, error: `no ${STORE_CONFIG_FILE} at the project root — see docs/store-plugin.md for the contract` };
  }
  let raw: unknown;
  try {
    raw = JSON.parse(readFileSync(path, "utf8"));
  } catch (e) {
    return { ok: false, error: `${STORE_CONFIG_FILE} is not valid JSON: ${(e as Error).message}` };
  }
  const parsed = StoreConfigSchema.safeParse(raw);
  if (!parsed.success) {
    const issues = parsed.error.issues.slice(0, 5).map((i) => `${i.path.join(".") || "(root)"}: ${i.message}`);
    return { ok: false, error: `${STORE_CONFIG_FILE} is invalid — ${issues.join("; ")}` };
  }
  const ids = new Set<string>();
  for (const app of parsed.data.apps) {
    if (ids.has(app.id)) return { ok: false, error: `${STORE_CONFIG_FILE}: duplicate app id "${app.id}"` };
    ids.add(app.id);
  }
  return { ok: true, config: parsed.data };
}

/**
 * Resolve an app's `package` glob to a concrete file. Supports `*`/`?` in the final path
 * segment only (e.g. `release/aurora/*.msixupload`); the newest match by mtime wins.
 */
export function resolvePackagePath(root: string, glob: string): { path: string } | { error: string } {
  const abs = isAbsolute(glob) ? glob : join(root, glob);
  if (!/[*?]/.test(glob)) {
    return existsSync(abs) ? { path: abs } : { error: `package not found: ${glob}` };
  }
  const dir = dirname(abs);
  const pattern = basename(abs);
  if (/[*?]/.test(dirname(glob).replace(/\\/g, "/"))) {
    return { error: `package glob "${glob}" — wildcards are only supported in the file name segment` };
  }
  let names: string[];
  try {
    names = readdirSync(dir);
  } catch {
    return { error: `package directory not found: ${dirname(glob)} — build the package first` };
  }
  const re = new RegExp(`^${pattern.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, "[^/\\\\]*").replace(/\?/g, ".")}$`, "i");
  const matches = names
    .filter((n) => re.test(n))
    .map((n) => {
      const p = join(dir, n);
      try {
        return { p, mtime: statSync(p).mtimeMs };
      } catch {
        return { p, mtime: 0 };
      }
    })
    .sort((a, b) => b.mtime - a.mtime);
  if (!matches.length) return { error: `no package matches ${glob} — build the package first` };
  return { path: matches[0]!.p };
}

/** Persist an add-on's Store id back into store.config.json (self-heal after create/drift). */
export function writeBackAddonStoreId(root: string, appId: string, storeId: string): boolean {
  const path = join(root, STORE_CONFIG_FILE);
  try {
    const raw = JSON.parse(readFileSync(path, "utf8")) as { apps?: { id?: string; addon?: { storeId?: string } }[] };
    const app = raw.apps?.find((a) => a.id === appId);
    if (!app?.addon) return false;
    app.addon.storeId = storeId;
    writeFileSync(path, JSON.stringify(raw, null, 2) + "\n", "utf8");
    return true;
  } catch {
    return false;
  }
}
