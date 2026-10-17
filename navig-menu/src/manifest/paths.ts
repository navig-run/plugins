import { join } from "node:path";

/** Per-project NAVIG state lives under `<root>/.navig/`. */
export function navigDir(root: string): string {
  return join(root, ".navig");
}

/** Generated detection cache — gitignored, fingerprint-keyed. */
export function cachePath(root: string): string {
  return join(navigDir(root), "menu.cache.json");
}

/** Human/AI-owned menu definition — versioned, highest precedence on merge. */
export function definitionPath(root: string): string {
  return join(navigDir(root), "menu.json");
}

/** Per-project recents (mirrors the prior-art scripts/menu.js `.menu-recent.json`). */
export function recentsPath(root: string): string {
  return join(navigDir(root), "menu.recent.json");
}

/** Local plugins (versioned): `.navig/plugins/*.{mjs,js,json,yaml,yml}`. */
export function pluginsDir(root: string): string {
  return join(navigDir(root), "plugins");
}

/** Plugin scratch/stat caches (gitignored): `.navig/plugins-cache/<plugin>/`. */
export function pluginCacheDir(root: string, plugin: string): string {
  return join(navigDir(root), "plugins-cache", plugin);
}
