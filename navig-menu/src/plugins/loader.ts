import { readdirSync } from "node:fs";
import { join, extname, basename } from "node:path";
import { pathToFileURL } from "node:url";
import { readJson, readYaml, exists } from "../detectors/fs.js";
import { pluginsDir } from "../manifest/paths.js";
import { BUILTIN_PLUGINS } from "./builtin/index.js";
import { DeclarativePluginSchema, declarativeToPlugin } from "./manifest.js";
import type { LoadedPlugin, MenuPlugin, PluginOrigin } from "./types.js";

// Matches `navig-menu-plugin-foo` and scoped `@you/navig-menu-plugin-foo`.
const NPM_PLUGIN_RE = /(^|\/)navig-menu-plugin-/;

/**
 * Synchronous discovery: bundled built-ins + local DECLARATIVE plugins (JSON/YAML). No dynamic
 * import, so this is safe from any sync caller (tests, `list`, the Bun binary). Programmatic local
 * / npm plugins are added by the async {@link loadPlugins}.
 */
export function loadPluginsSync(root: string): LoadedPlugin[] {
  const out: LoadedPlugin[] = BUILTIN_PLUGINS.map((plugin) => ({ plugin, origin: "builtin" as const }));
  out.push(...localDeclarative(root));
  return dedupe(out);
}

/**
 * Full async discovery: built-ins + local (declarative + programmatic) + npm packages. Dynamic
 * `import()` runs here (menu phase only). Every load is isolated — a broken plugin becomes an
 * inactive entry with an `error`, never a crash.
 */
export async function loadPlugins(
  root: string,
  deps: Record<string, string> = {},
  extraSpecifiers: string[] = [],
): Promise<LoadedPlugin[]> {
  const out: LoadedPlugin[] = BUILTIN_PLUGINS.map((plugin) => ({ plugin, origin: "builtin" as const }));

  // npm: packages matching the plugin naming convention, plus any explicit specifiers.
  const specifiers = [...new Set([...Object.keys(deps).filter((d) => NPM_PLUGIN_RE.test(d)), ...extraSpecifiers])];
  for (const spec of specifiers) out.push(await importPlugin(spec, "npm", spec));

  // local: .navig/plugins/*
  const dir = pluginsDir(root);
  if (exists(dir)) {
    let entries: string[] = [];
    try {
      entries = readdirSync(dir);
    } catch {
      entries = [];
    }
    for (const entry of entries.sort()) {
      const abs = join(dir, entry);
      const ext = extname(entry).toLowerCase();
      if (ext === ".json" || ext === ".yaml" || ext === ".yml") {
        out.push(loadDeclarativeFile(abs, entry));
      } else if (ext === ".mjs" || ext === ".js") {
        out.push(await importPlugin(pathToFileURL(abs).href, "local", entry));
      }
    }
  }

  return dedupe(out);
}

function localDeclarative(root: string): LoadedPlugin[] {
  const dir = pluginsDir(root);
  if (!exists(dir)) return [];
  let entries: string[] = [];
  try {
    entries = readdirSync(dir);
  } catch {
    return [];
  }
  const out: LoadedPlugin[] = [];
  for (const entry of entries.sort()) {
    const ext = extname(entry).toLowerCase();
    if (ext === ".json" || ext === ".yaml" || ext === ".yml") out.push(loadDeclarativeFile(join(dir, entry), entry));
  }
  return out;
}

function loadDeclarativeFile(abs: string, label: string): LoadedPlugin {
  const raw = extname(abs).toLowerCase() === ".json" ? readJson(abs) : readYaml(abs);
  const parsed = DeclarativePluginSchema.safeParse(raw);
  if (!parsed.success) {
    const first = parsed.error.issues[0];
    return errorEntry(label, "local", `${label}: ${first?.path.join(".")} — ${first?.message}`);
  }
  return { plugin: declarativeToPlugin(parsed.data), origin: "local" };
}

async function importPlugin(specifier: string, origin: PluginOrigin, label: string): Promise<LoadedPlugin> {
  try {
    const mod = (await import(specifier)) as { default?: MenuPlugin };
    const plugin = mod.default;
    if (!plugin || typeof plugin.id !== "string" || typeof plugin.contribute !== "function") {
      return errorEntry(label, origin, `${label}: not a valid plugin (missing default export with id + contribute)`);
    }
    return { plugin: { ...plugin, tier: plugin.tier ?? "programmatic" }, origin };
  } catch (e) {
    return errorEntry(label, origin, `${label}: ${(e as Error).message}`);
  }
}

function errorEntry(label: string, origin: PluginOrigin, error: string): LoadedPlugin {
  return {
    origin,
    error,
    plugin: { id: pluginId(label), tier: "programmatic", contribute: () => ({}) },
  };
}

function pluginId(label: string): string {
  return basename(label).replace(/\.(mjs|js|json|ya?ml)$/i, "");
}

/** Higher-precedence origin wins on id collision: local > npm > builtin. */
function dedupe(list: LoadedPlugin[]): LoadedPlugin[] {
  const rank: Record<PluginOrigin, number> = { builtin: 0, npm: 1, local: 2 };
  const byId = new Map<string, LoadedPlugin>();
  for (const item of list) {
    const prev = byId.get(item.plugin.id);
    if (!prev || rank[item.origin] >= rank[prev.origin]) byId.set(item.plugin.id, item);
  }
  return [...byId.values()];
}
