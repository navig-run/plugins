import { z } from "zod";
import {
  MenuDefinitionSchema,
  type Manifest,
  type MenuDefinition,
} from "../manifest/schema.js";
import { isLongRunning } from "./classify.js";
import { resolveCanonical } from "./canonical.js";

/**
 * Deterministic importer for an external, hand-curated command catalog → the engine's
 * `.navig/menu.json` definition. The reference format is `navig-catalog` v1 (the shape of
 * `scripts/menu.catalog.json`): `categories[] → items[]` where each item is `{ exec, cwd, label,
 * desc, danger }` or a TUI-only `special` handler.
 *
 * Why this exists: a mature project often already maintains a richer command list than can be
 * auto-detected from `package.json` scripts. Importing it gives the menu exact labels/descriptions
 * with zero AI/hallucination. Imported commands land in `extra[]` (catalog-owned, refreshed on
 * re-import); human curation belongs in `overrides[]`, which wins later at merge time — so a
 * re-import never clobbers a hand edit. Idempotent: same catalog in → same definition out.
 */

const CatalogItemSchema = z.object({
  id: z.string(),
  label: z.string(),
  desc: z.string().optional(),
  exec: z.string().optional(),
  cwd: z.string().optional(),
  danger: z.boolean().optional(),
  /** A TUI-only interactive handler (license mint, per-extension picker, …). Not a shell command. */
  special: z.string().optional(),
});

const CatalogCategorySchema = z.object({
  key: z.string().optional(),
  icon: z.string().optional(),
  label: z.string(),
  blurb: z.string().optional(),
  items: z.array(CatalogItemSchema).default([]),
});

/** The `navig-catalog` v1 file format (scripts/menu.catalog.json). */
export const NavigCatalogSchema = z.object({
  version: z.number().optional(),
  categories: z.array(CatalogCategorySchema).default([]),
});
export type NavigCatalog = z.infer<typeof NavigCatalogSchema>;

export interface ImportAudit {
  /** Catalog item ids added/refreshed as `extra[]` actions. */
  imported: string[];
  /** Section names emitted to `groups[]`, in catalog order. */
  groups: string[];
  /** Detected script ids hidden because a catalog item runs the same command. */
  demoted: string[];
  /** Item ids skipped: `exec` contains shell operators the shell-free runner can't execute. */
  skippedShell: string[];
  /** Item ids skipped: interactive `special` handlers with no shell equivalent. */
  skippedSpecial: string[];
  /** Item ids skipped: neither `exec` nor `special`. */
  skippedEmpty: string[];
  /** Item ids skipped: a duplicate id already imported (first one wins). */
  skippedDuplicate: string[];
}

export interface ImportResult {
  definition: MenuDefinition;
  audit: ImportAudit;
}

/** Section tone by catalog category key; falls back to the neutral accent. */
const TONE_BY_KEY: Record<string, string> = {
  quickstart: "cyan",
  dev: "blue",
  checks: "yellow",
  build: "green",
  forge: "magenta",
  admin: "red",
  deploy: "red",
  shell: "purple",
  git: "cyan",
};

/**
 * Shell operators the argv-array runner cannot execute (no shell). An item using any of these
 * (chaining, pipes, redirects, subshells) is skipped rather than silently mis-run. `--` (npm arg
 * separator) and single `-flags` are NOT operators and stay.
 */
const SHELL_OPERATORS = /(&&|\|\||[|;`]|(?:^|\s)[<>])/;

export function hasShellOperators(cmd: string): boolean {
  return SHELL_OPERATORS.test(cmd);
}

/** Normalize a directory to a comparable form (forward slashes, no leading `./` or trailing `/`). */
function normDir(dir: string | undefined): string {
  if (!dir) return ".";
  const d = dir.replace(/\\/g, "/").replace(/^\.\//, "").replace(/\/+$/, "");
  return d || ".";
}

/** Collapse whitespace so two spellings of the same command compare equal. */
function normCmd(cmd: string): string {
  return cmd.trim().replace(/\s+/g, " ");
}

/**
 * Decompose a raw script into (dir, command) by stripping a leading `cd <dir> && …` (or `; …`).
 * This is what lets us dedup a detected root wrapper (`cd apps/deck && npm run dev`) against a
 * catalog item that runs the same command with an explicit `cwd` — matching by what actually runs,
 * not by how the script id happens to be spelled.
 */
export function parseCwdCommand(raw: string): { cwd: string; cmd: string } {
  const m = /^\s*cd\s+("[^"]+"|'[^']+'|\S+)\s*(?:&&|;)\s*(.+)$/.exec(raw);
  if (m) {
    const dir = m[1]!.replace(/^["']|["']$/g, "");
    return { cwd: normDir(dir), cmd: normCmd(m[2]!) };
  }
  return { cwd: ".", cmd: normCmd(raw) };
}

/** The command-equivalence key used for dedup: where it runs + what it runs. */
function equivKey(cwd: string | undefined, cmd: string): string {
  return `${normDir(cwd)}\0${normCmd(cmd)}`;
}

/** First token of an id that resolves to a canonical action (dev/build/test/…), else undefined. */
function canonicalOfId(id: string): string | undefined {
  for (const tok of id.split(/[:_\-./]+/)) {
    const c = resolveCanonical(tok);
    if (c) return c;
  }
  return undefined;
}

/**
 * Intent key for the secondary dedup: same package dir + same canonical verb. Catches a stale
 * detected root script (`dev:os` → `cd apps/os && next dev`) whose command has drifted from the
 * catalog's current one (`os:dev` → `bun run electron:dev`) — same intent, so the catalog wins.
 */
function intentKey(dir: string | undefined, canonical: string | undefined): string | undefined {
  return canonical ? `${normDir(dir)}#${canonical}` : undefined;
}

/**
 * Translate a catalog into a menu definition, merged over `existing` (human edits preserved).
 * Pass `manifest` to demote detected root scripts that a catalog item supersedes.
 */
export function importCatalog(
  catalog: NavigCatalog,
  existing: MenuDefinition = {},
  manifest?: Manifest,
): ImportResult {
  const audit: ImportAudit = {
    imported: [],
    groups: [],
    demoted: [],
    skippedShell: [],
    skippedSpecial: [],
    skippedEmpty: [],
    skippedDuplicate: [],
  };

  // ── Groups: catalog order, preserving any human-edited group of the same name ──
  const existingGroups = existing.groups ?? [];
  const existingByName = new Map(existingGroups.map((g) => [g.name, g]));
  const emittedNames = new Set<string>();
  const groups: NonNullable<MenuDefinition["groups"]> = [];
  for (const cat of catalog.categories) {
    const name = cat.label.toUpperCase();
    if (emittedNames.has(name)) continue;
    emittedNames.add(name);
    const preserved = existingByName.get(name);
    groups.push(
      preserved ?? {
        name,
        title: name,
        icon: cat.icon,
        tone: TONE_BY_KEY[cat.key ?? ""] ?? "accent",
      },
    );
    audit.groups.push(name);
  }
  // Append human-added groups that aren't in the catalog (keep their custom sections + order).
  for (const g of existingGroups) if (!emittedNames.has(g.name)) groups.push(g);

  // ── Extras: catalog-owned runnable items (refreshed by id) ──
  const catalogExtras: NonNullable<MenuDefinition["extra"]> = [];
  const catalogIds = new Set<string>();
  const demoteKeys = new Set<string>();
  const intentKeys = new Set<string>();
  for (const cat of catalog.categories) {
    const group = cat.label.toUpperCase();
    for (const item of cat.items) {
      if (item.special) {
        audit.skippedSpecial.push(item.id);
        continue;
      }
      if (!item.exec) {
        audit.skippedEmpty.push(item.id);
        continue;
      }
      if (hasShellOperators(item.exec)) {
        audit.skippedShell.push(item.id);
        continue;
      }
      if (catalogIds.has(item.id)) {
        audit.skippedDuplicate.push(item.id); // first import of an id wins; avoid a doubled row
        continue;
      }
      const cmd = normCmd(item.exec);
      const longRunning =
        cat.key === "dev" || isLongRunning(item.id, cmd) || /\bgateway\b/.test(cmd);
      catalogExtras.push({
        id: item.id,
        label: item.label,
        cmd,
        ...(item.desc ? { description: item.desc } : {}),
        group,
        ...(item.danger ? { risk: "dangerous" as const } : {}),
        ...(longRunning ? { longRunning: true } : {}),
        ...(item.cwd && normDir(item.cwd) !== "." ? { cwd: normDir(item.cwd) } : {}),
        why: "imported from menu catalog",
      });
      catalogIds.add(item.id);
      demoteKeys.add(equivKey(item.cwd, cmd));
      const ik = intentKey(item.cwd, canonicalOfId(item.id));
      if (ik) intentKeys.add(ik);
    }
  }
  audit.imported = [...catalogIds];

  // Keep human-authored extras (ids the catalog doesn't own), then the fresh catalog set.
  const humanExtras = (existing.extra ?? []).filter((e) => !catalogIds.has(e.id));
  const extra = [...humanExtras, ...catalogExtras];

  // ── Demote detected duplicates ──
  // A detected root script is hidden when a catalog item either runs the same command (exact) or
  // covers the same package + canonical verb (intent) — so the curated version wins and the menu
  // isn't doubled up. Intent-demote also sweeps stale root wrappers whose command has drifted.
  const demoted: string[] = [];
  if (manifest) {
    for (const [id, raw] of Object.entries(manifest.scripts)) {
      const { cwd, cmd } = parseCwdCommand(raw);
      const ik = intentKey(cwd, canonicalOfId(id));
      if (demoteKeys.has(equivKey(cwd, cmd)) || (ik && intentKeys.has(ik))) demoted.push(id);
    }
  }
  audit.demoted = demoted;
  const hide = [...new Set([...(existing.hide ?? []), ...demoted])];

  // Prune inert overrides: a prior `overrides` entry that now targets a hidden script or an id that
  // no longer exists (not a detected script and not an extra) does nothing — drop it so the file
  // stays honest. Overrides that still curate a visible item are kept (human curation wins on merge).
  const hideSet = new Set(hide);
  const liveIds = new Set<string>([...Object.keys(manifest?.scripts ?? {}), ...extra.map((e) => e.id)]);
  const overrides = existing.overrides
    ? Object.fromEntries(
        Object.entries(existing.overrides).filter(([id]) => !hideSet.has(id) && liveIds.has(id)),
      )
    : undefined;

  const definition = MenuDefinitionSchema.parse({
    ...existing,
    $schema: existing.$schema ?? "https://navig.run/schema/v1.json",
    groups: groups.length ? groups : undefined,
    extra: extra.length ? extra : undefined,
    hide: hide.length ? hide : undefined,
    overrides: overrides && Object.keys(overrides).length ? overrides : undefined,
  });

  return { definition, audit };
}
