import { readFileSync, readdirSync, statSync, existsSync } from "node:fs";
import { join, relative } from "node:path";
import { parse as parseYaml } from "yaml";
import { IGNORE_DIRS, MAX_SCANNED_FILES } from "../config/constants.js";

/** A file we found during the shallow walk, with cheap stat metadata for fingerprinting. */
export interface ScannedFile {
  /** Path relative to root, POSIX-normalized. */
  rel: string;
  abs: string;
  mtimeMs: number;
  size: number;
}

export interface ScanResult {
  root: string;
  files: ScannedFile[];
  truncated: boolean;
}

/** Read a UTF-8 file or return undefined (never throws — callers treat absence as "no evidence"). */
export function readText(abs: string): string | undefined {
  try {
    return readFileSync(abs, "utf8");
  } catch {
    return undefined;
  }
}

export function readJson<T = unknown>(abs: string): T | undefined {
  const raw = readText(abs);
  if (raw === undefined) return undefined;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return undefined;
  }
}

export function readYaml<T = unknown>(abs: string): T | undefined {
  const raw = readText(abs);
  if (raw === undefined) return undefined;
  try {
    return parseYaml(raw) as T;
  } catch {
    return undefined;
  }
}

export function exists(abs: string): boolean {
  return existsSync(abs);
}

/**
 * Shallow-first directory walk. Skips IGNORE_DIRS, bounds depth, and caps total files so a
 * pathological tree degrades gracefully (truncated=true) instead of hanging. Detection runs
 * over these paths — we never execute anything here.
 */
export function walk(root: string, maxDepth: number): ScanResult {
  const files: ScannedFile[] = [];
  let truncated = false;

  const visit = (dir: string, depth: number): void => {
    if (truncated || depth > maxDepth) return;
    let entries: string[];
    try {
      entries = readdirSync(dir);
    } catch {
      return;
    }
    for (const entry of entries) {
      if (files.length >= MAX_SCANNED_FILES) {
        truncated = true;
        return;
      }
      const abs = join(dir, entry);
      let st;
      try {
        st = statSync(abs);
      } catch {
        continue;
      }
      if (st.isDirectory()) {
        if (IGNORE_DIRS.has(entry) || entry.startsWith(".") && entry !== ".github") {
          // Hidden dirs are mostly tooling caches; .github is the one we care about.
          if (entry !== ".navig") continue;
        }
        visit(abs, depth + 1);
      } else if (st.isFile()) {
        files.push({
          rel: relative(root, abs).split("\\").join("/"),
          abs,
          mtimeMs: Math.floor(st.mtimeMs),
          size: st.size,
        });
      }
    }
  };

  visit(root, 0);
  return { root, files, truncated };
}

/** Filenames/paths that are "interesting" for detection — used to scope the fingerprint. */
export function isInterestingFile(rel: string): boolean {
  const base = rel.split("/").pop() ?? rel;
  if (INTERESTING_BASENAMES.has(base)) return true;
  return INTERESTING_PATTERNS.some((re) => re.test(base));
}

const INTERESTING_BASENAMES = new Set<string>([
  "package.json",
  "package-lock.json",
  "pnpm-lock.yaml",
  "yarn.lock",
  "bun.lockb",
  "bun.lock",
  "pnpm-workspace.yaml",
  "turbo.json",
  "nx.json",
  "tsconfig.json",
  "composer.json",
  "pyproject.toml",
  "Cargo.toml",
  "go.mod",
  "Dockerfile",
  "artisan",
  "tauri.conf.json",
]);

const INTERESTING_PATTERNS: RegExp[] = [
  /^docker-compose.*\.ya?ml$/i,
  /^wrangler\.(toml|jsonc?|ya?ml)$/i,
  /^(next|vite|nuxt|astro|svelte|vue|remix)\.config\.[mc]?[jt]s$/i,
  /^tauri\..*\.conf\.json$/i,
  /^\.env(\..+)?$/i,
];
