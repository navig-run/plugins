import { join } from "node:path";
import type { ScanResult } from "./fs.js";
import { readJson, exists } from "./fs.js";

export interface PackageJson {
  name?: string;
  description?: string;
  version?: string;
  license?: string;
  packageManager?: string;
  scripts?: Record<string, string>;
  dependencies?: Record<string, string>;
  devDependencies?: Record<string, string>;
  optionalDependencies?: Record<string, string>;
  peerDependencies?: Record<string, string>;
  workspaces?: string[] | { packages?: string[] };
}

/** Everything a detector needs, parsed once. Detectors are pure reads over this. */
export interface DetectContext {
  root: string;
  scan: ScanResult;
  pkg?: PackageJson;
  /** Union of all dependency buckets (name → range). Presence is what we key on. */
  deps: Record<string, string>;
  relSet: Set<string>;
  hasFile(rel: string): boolean;
  absExists(rel: string): boolean;
}

export function makeContext(root: string, scan: ScanResult): DetectContext {
  const pkg = readJson<PackageJson>(join(root, "package.json"));
  const deps: Record<string, string> = {
    ...(pkg?.dependencies ?? {}),
    ...(pkg?.devDependencies ?? {}),
    ...(pkg?.optionalDependencies ?? {}),
    ...(pkg?.peerDependencies ?? {}),
  };
  const relSet = new Set(scan.files.map((f) => f.rel));
  return {
    root,
    scan,
    pkg,
    deps,
    relSet,
    hasFile: (rel) => relSet.has(rel),
    absExists: (rel) => exists(join(root, rel)),
  };
}

/** True if any dependency name matches one of the given exact names or prefixes (`@scope/`). */
export function hasDep(ctx: DetectContext, ...names: string[]): boolean {
  return names.some((n) =>
    n.endsWith("/")
      ? Object.keys(ctx.deps).some((d) => d.startsWith(n))
      : ctx.deps[n] !== undefined,
  );
}
