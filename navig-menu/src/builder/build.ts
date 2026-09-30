import { writeFileSync, mkdirSync, readFileSync } from "node:fs";
import { relative } from "node:path";
import { walk } from "../detectors/fs.js";
import { makeContext } from "../detectors/context.js";
import { detectPackageManager } from "../detectors/packageManager.js";
import { detectWorkspace, resolveWorkspacePackages } from "../detectors/workspace.js";
import { detectFrameworks } from "../detectors/frameworks.js";
import { detectServices } from "../detectors/services.js";
import { detectEndpoints } from "../detectors/endpoints.js";
import { fingerprint } from "../manifest/fingerprint.js";
import { cachePath, definitionPath, navigDir } from "../manifest/paths.js";
import {
  ManifestSchema,
  MenuDefinitionSchema,
  type Manifest,
  type MenuDefinition,
  type Action,
  type Warning,
  type MenuUi,
  type Endpoint,
} from "../manifest/schema.js";
import { ProjectScriptsSource } from "../sources/project-scripts.js";
import { applyDefinition } from "./merge.js";
import { GROUPS, registerSectionMeta, type Group } from "./classify.js";
import { projectFor } from "./project.js";
import { loadPluginsSync } from "../plugins/loader.js";
import { applyPlugins } from "../plugins/apply.js";
import type {
  BannerLine,
  LoadedPlugin,
  PluginSummary,
  ResolvedPluginSetting,
  TextOverrides,
} from "../plugins/types.js";
import { SCHEMA_VERSION, TOOL_NAME, TOOL_VERSION, DEFAULT_SCAN_DEPTH, DEEP_SCAN_DEPTH } from "../config/constants.js";

export interface ScanOptions {
  deep?: boolean;
  noCache?: boolean;
}

/** Full detection pass → Manifest. Pure reads; never executes project code. */
export function scanProject(root: string, opts: ScanOptions = {}): Manifest {
  const depth = opts.deep ? DEEP_SCAN_DEPTH : DEFAULT_SCAN_DEPTH;
  const scan = walk(root, depth);
  const ctx = makeContext(root, scan);

  const pmRes = detectPackageManager(ctx);
  const workspace = detectWorkspace(ctx);
  const frameworks = detectFrameworks(ctx);
  const services = detectServices(ctx);

  // Resolve workspace package dirs (relative) for a meaningful count.
  const resolved = resolveWorkspacePackages(ctx, workspace).map((d) =>
    relative(root, d).split("\\").join("/"),
  );
  if (resolved.length) workspace.packages = resolved;

  const endpoints = detectEndpoints(ctx, workspace.packages);
  const docs = scan.files.filter((f) => /\.mdx?$/i.test(f.rel)).length;

  const warnings: Warning[] = [...pmRes.warnings];
  if (scan.truncated) {
    warnings.push({ code: "scan_truncated", detail: "file cap reached; results may be partial (use a narrower cwd)" });
  }

  const source = new ProjectScriptsSource(pmRes.info.value, frameworks);
  const finding = source.detect(ctx);
  if (finding.warnings) warnings.push(...finding.warnings);

  const manifest: Manifest = {
    schemaVersion: SCHEMA_VERSION,
    generatedAt: new Date().toISOString(),
    tool: { name: TOOL_NAME, version: TOOL_VERSION },
    fingerprint: fingerprint(scan),
    root,
    name: ctx.pkg?.name,
    version: ctx.pkg?.version,
    license: ctx.pkg?.license,
    purpose: ctx.pkg?.description
      ? { value: ctx.pkg.description, confidence: "detected" }
      : undefined,
    packageManager: pmRes.info,
    workspace,
    frameworks,
    services,
    endpoints,
    docs,
    actions: finding.actions,
    scripts: finding.scripts ?? {},
    warnings,
  };
  return ManifestSchema.parse(manifest);
}

/** Reuse the cached manifest when the fingerprint matches; otherwise rescan + rewrite. */
export function getManifest(root: string, opts: ScanOptions = {}): Manifest {
  if (!opts.noCache) {
    const cached = readCache(root);
    if (cached && cached.schemaVersion === SCHEMA_VERSION) {
      const current = scanFingerprint(root, opts);
      if (cached.fingerprint === current) return cached;
    }
  }
  const fresh = scanProject(root, opts);
  writeCache(root, fresh);
  return fresh;
}

function scanFingerprint(root: string, opts: ScanOptions): string {
  const depth = opts.deep ? DEEP_SCAN_DEPTH : DEFAULT_SCAN_DEPTH;
  return fingerprint(walk(root, depth));
}

export function readCache(root: string): Manifest | undefined {
  try {
    const raw = readFileSync(cachePath(root), "utf8");
    return ManifestSchema.parse(JSON.parse(raw));
  } catch {
    return undefined;
  }
}

export function writeCache(root: string, manifest: Manifest): void {
  try {
    mkdirSync(navigDir(root), { recursive: true });
    writeFileSync(cachePath(root), JSON.stringify(manifest, null, 2) + "\n", "utf8");
  } catch {
    /* cache is best-effort; a read-only cwd just means we rescan each time */
  }
}

export interface DefinitionLoad {
  def?: MenuDefinition;
  error?: string;
}

/** Load + validate the human/AI override. Invalid JSON/schema → precise error, graceful fallback. */
export function loadDefinition(root: string): DefinitionLoad {
  let raw: string;
  try {
    raw = readFileSync(definitionPath(root), "utf8");
  } catch {
    return {}; // absent is normal
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (e) {
    return { error: `.navig/menu.json is not valid JSON: ${(e as Error).message}` };
  }
  const res = MenuDefinitionSchema.safeParse(parsed);
  if (!res.success) {
    const first = res.error.issues[0];
    return { error: `.navig/menu.json invalid: ${first?.path.join(".")} — ${first?.message}` };
  }
  return { def: res.data };
}

/* ── Menu model (what the UI renders) ──────────────────────────────────────── */

export interface MenuModel {
  root: string;
  title: string;
  purpose?: string;
  version?: string;
  license?: string;
  accent?: string;
  packageManager: string;
  pmConfidence: string;
  frameworks: string[];
  services: { id: string; confidence: string }[];
  endpoints: Endpoint[];
  packages: string[];
  workspaceKind: string;
  stats: { scripts: number; packages: number; services: number; docs: number };
  groups: { group: Group; items: Action[] }[];
  allScripts: Action[];
  warnings: Warning[];
  defError?: string;
  ui?: MenuUi;
  /* ── plugin contributions (folded in the model phase; never in the manifest) ── */
  textOverrides?: TextOverrides;
  bannerLines?: BannerLine[];
  bannerPhrases?: string[];
  about?: string[];
  pluginSettings?: ResolvedPluginSetting[];
  loadedPlugins?: PluginSummary[];
}

/**
 * Build the render model. Plugins fold in here (model phase) — never during the pure scan. Pass a
 * pre-loaded `plugins` list (from the async loader in commands) to include programmatic local/npm
 * plugins; without it we use the sync loader (built-ins + local declarative), which is enough for
 * tests, `list`, and the compiled binary.
 */
export function buildMenuModel(manifest: Manifest, load: DefinitionLoad, plugins?: LoadedPlugin[]): MenuModel {
  const def = load.def;
  const merged0 = applyDefinition(manifest.actions, def);
  const loaded = plugins ?? loadPluginsSync(manifest.root);
  const applied = applyPlugins({ root: manifest.root, manifest, actions: merged0, state: def?.plugins, loaded });
  const merged = applied.actions;

  // Description fallback: an un-curated detected script shows the command it actually runs (from
  // `manifest.scripts`, e.g. "cd apps/deck && npm run dev") instead of nothing — so the menu is
  // never a wall of bare ids. Human descriptions and imported/extra items already have their own.
  // Also tag each action with its owning project (for the per-project view).
  for (const a of merged) {
    if (!a.description && manifest.scripts[a.id]) a.description = manifest.scripts[a.id];
    if (!a.project) a.project = projectFor(a.id, a.label, manifest.workspace.packages);
  }

  // Custom section presentation (glyph/tone/title) from the definition.
  for (const g of def?.groups ?? []) {
    registerSectionMeta(g.name, { title: g.title, emoji: g.icon, unicode: g.icon, ascii: g.ascii, tone: g.tone });
  }

  // Order sections: definition `groups` order first, then canonical GROUPS, then any leftovers.
  const defOrder = (def?.groups ?? []).map((g) => g.name);
  const canon = GROUPS as readonly string[];
  const rankOf = (name: string): number => {
    const di = defOrder.indexOf(name);
    if (di >= 0) return di;
    const gi = canon.indexOf(name);
    return gi >= 0 ? defOrder.length + gi : defOrder.length + canon.length;
  };
  const present = [...new Set(merged.map((a) => a.group))].sort((a, b) => rankOf(a) - rankOf(b));
  const groups = present
    .map((name) => ({ group: name as Group, items: merged.filter((a) => a.group === name) }))
    .filter((g) => g.items.length > 0);

  return {
    root: manifest.root,
    title: def?.title ?? manifest.name ?? baseName(manifest.root),
    purpose: def?.purpose ?? manifest.purpose?.value,
    version: manifest.version,
    license: manifest.license,
    accent: def?.accent,
    packageManager: def?.packageManager ?? manifest.packageManager.value,
    pmConfidence: manifest.packageManager.confidence,
    frameworks: manifest.frameworks.map((f) => f.id),
    services: manifest.services.map((s) => ({ id: s.id, confidence: s.confidence })),
    endpoints: manifest.endpoints,
    packages: manifest.workspace.packages,
    workspaceKind: manifest.workspace.kind,
    stats: {
      scripts: Object.keys(manifest.scripts).length,
      packages: manifest.workspace.packages.length,
      services: manifest.services.length,
      docs: manifest.docs,
    },
    groups,
    allScripts: merged,
    warnings: [...manifest.warnings, ...applied.warnings],
    defError: load.error,
    ui: def?.ui,
    textOverrides: Object.keys(applied.text).length ? applied.text : undefined,
    bannerLines: applied.bannerLines.length ? applied.bannerLines : undefined,
    bannerPhrases: applied.bannerPhrases.length ? applied.bannerPhrases : undefined,
    about: applied.about.length ? applied.about : undefined,
    pluginSettings: applied.pluginSettings.length ? applied.pluginSettings : undefined,
    loadedPlugins: applied.loaded.length ? applied.loaded : undefined,
  };
}

function baseName(p: string): string {
  return p.split(/[\\/]/).filter(Boolean).pop() ?? p;
}
