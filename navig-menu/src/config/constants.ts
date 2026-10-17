/** Build/version stamp surfaced by `--version` and the manifest `tool` field. */
export const TOOL_NAME = "navig-menu";
export const TOOL_VERSION = "1.3.0";
export const SCHEMA_VERSION = 3;

/** Identity surfaced by the About screen. */
export const STUDIO = "Cybesis Studio";
export const PROJECT_URL = "https://navig.run";
export const AUTHOR_GITHUB = "github.com/miztizm";
export const LICENSE = "MIT";

/** Directories never worth walking — perf + correctness (e.g. node_modules dep trees). */
export const IGNORE_DIRS = new Set<string>([
  "node_modules",
  ".git",
  "dist",
  "build",
  "out",
  ".next",
  ".turbo",
  ".nuxt",
  ".svelte-kit",
  ".output",
  "target",
  ".venv",
  "venv",
  "__pycache__",
  ".cache",
  ".pnpm-store",
  "coverage",
  "vendor",
  ".idea",
  ".vscode",
]);

/**
 * Canonical action keys. The builder maps these onto whatever a repo actually
 * calls them — this is how we "standardize actions" without editing package.json.
 */
export const CANONICAL_ACTIONS = [
  "dev",
  "build",
  "test",
  "typecheck",
  "lint",
  "lint:fix",
  "preview",
  "start",
  "deploy",
  "migrate",
  "seed",
  "clean",
  "reset",
] as const;
export type CanonicalAction = (typeof CANONICAL_ACTIONS)[number];

/** Hard cap so a pathological tree degrades to a warning instead of hanging. */
export const MAX_SCANNED_FILES = 20_000;

/**
 * Shallow-scan depth; `--deep` lifts this to walk workspace package globs fully. 6 is deep enough
 * to reach framework route conventions (e.g. Next.js `app/api/webhooks/stripe/route.ts`) while the
 * IGNORE_DIRS set + the file cap keep it fast. `--deep` goes further for large monorepos.
 */
export const DEFAULT_SCAN_DEPTH = 6;
export const DEEP_SCAN_DEPTH = 12;
