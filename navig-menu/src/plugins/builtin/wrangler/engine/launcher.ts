/**
 * Resolve how to invoke `wrangler`. The reliable path is the project's own local install
 * (`node_modules/.bin/wrangler`), which pins the exact version the repo uses; when that's missing we
 * fall back to `npx --yes wrangler` (npx ships with Node, so it's always available and won't hang on
 * an install prompt). A `runner` setting lets the user force `npx`/`pnpm`/`bun` instead.
 */

import { existsSync } from "node:fs";
import { join } from "node:path";

export interface WranglerInvocation {
  launcher: string;
  /** Tokens inserted before the command args (e.g. `["--yes","wrangler"]` for npx). */
  prefix: string[];
  source: "local" | "npx" | "pnpm" | "bun";
}

/** Absolute path to a project-local wrangler bin shim, or undefined. */
export function findLocalWrangler(root: string): string | undefined {
  const bin = process.platform === "win32" ? "wrangler.cmd" : "wrangler";
  const p = join(root, "node_modules", ".bin", bin);
  return existsSync(p) ? p : undefined;
}

export function resolveWranglerInvocation(root: string, runner = "auto"): WranglerInvocation {
  const npx: WranglerInvocation = { launcher: "npx", prefix: ["--yes", "wrangler"], source: "npx" };
  const local = (): WranglerInvocation | undefined => {
    const b = findLocalWrangler(root);
    return b ? { launcher: b, prefix: [], source: "local" } : undefined;
  };

  switch (runner) {
    case "npx":
      return npx;
    case "pnpm":
      return { launcher: "pnpm", prefix: ["dlx", "wrangler"], source: "pnpm" };
    case "bun":
      return { launcher: "bunx", prefix: ["wrangler"], source: "bun" };
    default: // "auto" / "local" / anything else — prefer the pinned local bin, else npx.
      return local() ?? npx;
  }
}
