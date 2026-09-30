import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { execa } from "execa";
import { readJson, exists } from "../detectors/fs.js";
import { pluginCacheDir } from "../manifest/paths.js";
import { registerSectionMeta } from "../builder/classify.js";
import { tokenize } from "../builder/merge.js";
import { runActionLocal } from "../runners/local.js";
import { resolveLauncher } from "../util/which.js";
import { select as selectPrompt, input as inputPrompt, confirmPrompt, CANCEL } from "../ui/prompts.js";
import type { Action, Manifest, PluginState, Warning } from "../manifest/schema.js";
import type { Theme } from "../ui/theme.js";
import type {
  BannerLine,
  LoadedPlugin,
  MenuPlugin,
  PluginActionContext,
  PluginApply,
  PluginContext,
  PluginContribution,
  PluginSummary,
  ResolvedPluginSetting,
  ScriptClaim,
  SectionContribution,
  TextOverrides,
} from "./types.js";

/**
 * Fold every active plugin's contributions into the menu. Pure w.r.t. the filesystem except reading
 * package.json (deps) and plugin caches; the pure detection scan never calls this.
 */
export function applyPlugins(input: {
  root: string;
  manifest: Manifest;
  actions: Action[];
  state: PluginState | undefined;
  loaded: LoadedPlugin[];
}): PluginApply {
  const { root, manifest, loaded } = input;
  const state = input.state ?? {};
  const deps = readDeps(root);

  // Work on clones so we never mutate the shared manifest actions across rebuilds.
  let actions: Action[] = input.actions.map((a) => ({ ...a }));

  const text: TextOverrides = {};
  const bannerLines: BannerLine[] = [];
  const bannerPhrases: string[] = [];
  const about: string[] = [];
  const pluginSettings: ResolvedPluginSetting[] = [];
  const claims: ScriptClaim[] = [];
  const sectionSpecs: Array<{ pluginId: string; section: SectionContribution }> = [];
  const summaries: PluginSummary[] = [];
  const warnings: Warning[] = [];

  for (const { plugin, origin, error } of loaded) {
    if (error) {
      summaries.push({ id: plugin.id, tier: plugin.tier, origin, version: plugin.version, active: false, auto: false, error });
      warnings.push({ code: "plugin_error", detail: error });
      continue;
    }
    const explicit = state[plugin.id]?.enabled;
    const detected = explicit === undefined && plugin.detect ? safeDetect(plugin, ctx(root, manifest, deps, {}, plugin.id)) : false;
    const active = explicit ?? detected;
    summaries.push({ id: plugin.id, tier: plugin.tier, origin, version: plugin.version, active, auto: active && explicit === undefined });
    if (!active) continue;

    // Two-pass so a plugin's output can depend on its own settings.
    const c0 = safeContribute(plugin, ctx(root, manifest, deps, {}, plugin.id), warnings);
    const specs = c0.settings ?? [];
    let contribution = c0;
    if (specs.length) {
      const resolved: Record<string, string | boolean> = {};
      for (const spec of specs) resolved[spec.key] = state[plugin.id]?.settings?.[spec.key] ?? spec.default;
      contribution = safeContribute(plugin, ctx(root, manifest, deps, resolved, plugin.id), warnings);
      for (const spec of specs) {
        pluginSettings.push({ pluginId: plugin.id, key: spec.key, label: spec.label, type: spec.type, values: spec.values, value: resolved[spec.key]! });
      }
    }

    if (contribution.text) Object.assign(text, clean(contribution.text));
    if (contribution.bannerLines) bannerLines.push(...contribution.bannerLines);
    if (contribution.bannerPhrases) bannerPhrases.push(...contribution.bannerPhrases);
    if (contribution.about) about.push(...contribution.about);
    if (contribution.claims) claims.push(...contribution.claims);
    for (const section of contribution.sections ?? []) {
      if (section.meta) registerSectionMeta(section.group, section.meta);
      sectionSpecs.push({ pluginId: plugin.id, section });
    }
  }

  actions = applyClaims(actions, claims);
  const byId = new Map(actions.map((a) => [a.id, a]));
  for (const { pluginId, section } of sectionSpecs) {
    for (const spec of section.actions) {
      const built = buildAction(pluginId, section.group, spec, byId, warnings);
      if (built) actions.push(built);
    }
  }

  return { actions, text, bannerLines, bannerPhrases, about, pluginSettings, loaded: summaries, warnings };
}

/* ── context ───────────────────────────────────────────────────────────────────── */

function ctx(
  root: string,
  manifest: Manifest,
  deps: Record<string, string>,
  settings: Record<string, string | boolean>,
  pluginId: string,
): PluginContext {
  const dir = pluginCacheDir(root, pluginId);
  return {
    root,
    manifest,
    scripts: manifest.scripts,
    settings,
    cacheDir: dir,
    hasFile: (rel) => exists(join(root, rel)),
    hasDep: (...names) =>
      names.some((n) => (n.endsWith("/") ? Object.keys(deps).some((d) => d.startsWith(n)) : deps[n] !== undefined)),
    readCache: (name) => readJson(join(dir, cacheFile(name))),
  };
}

/** Build the menu-phase action context for a plugin handler (sanctioned side effects). */
export function makeActionContext(opts: {
  root: string;
  manifest: Manifest;
  pluginId: string;
  settings: Record<string, string | boolean>;
  theme: Theme;
  notify: (message: string) => void;
}): PluginActionContext {
  const deps = readDeps(opts.root);
  const dir = pluginCacheDir(opts.root, opts.pluginId);
  const base = ctx(opts.root, opts.manifest, deps, opts.settings, opts.pluginId);
  return {
    ...base,
    theme: opts.theme,
    notify: opts.notify,
    run: async (launcher, argv, cwd) => {
      const res = await runActionLocal(
        { id: "plugin", label: launcher, group: "", launcher, argv, cwd: cwd ?? ".", risk: "safe", longRunning: false, confidence: "explicit", evidence: [], source: "plugin" },
        opts.root,
      );
      return res.exitCode;
    },
    capture: async (launcher, argv, cwd) => {
      try {
        const res = await execa(resolveLauncher(launcher), argv, {
          cwd: cwd ? join(opts.root, cwd) : opts.root,
          reject: false,
          all: false,
          windowsHide: true,
        });
        return { stdout: res.stdout ?? "", exitCode: res.exitCode ?? 1 };
      } catch {
        return { stdout: "", exitCode: 1 };
      }
    },
    select: async (o) => {
      const r = await selectPrompt(o);
      return r === CANCEL ? undefined : r;
    },
    input: async (message) => {
      const r = await inputPrompt(message);
      return r === CANCEL ? undefined : r;
    },
    confirm: async (message) => {
      const r = await confirmPrompt(message, false);
      return r === CANCEL ? false : r;
    },
    writeCache: (name, data) => {
      try {
        mkdirSync(dir, { recursive: true });
        writeFileSync(join(dir, cacheFile(name)), JSON.stringify(data, null, 2) + "\n", "utf8");
      } catch {
        /* caches are best-effort */
      }
    },
  };
}

/** Dispatch an internal plugin action's handler with full error isolation. */
export async function runPluginHandler(
  loaded: LoadedPlugin[],
  pluginId: string,
  handlerKey: string,
  actx: PluginActionContext,
): Promise<void> {
  const plugin = loaded.find((l) => l.plugin.id === pluginId)?.plugin;
  const handler = plugin?.handlers?.[handlerKey];
  if (!handler) {
    actx.notify(`no handler "${handlerKey}" in plugin "${pluginId}"`);
    return;
  }
  try {
    await handler(actx);
  } catch (e) {
    actx.notify(`plugin ${pluginId} failed: ${(e as Error).message}`);
  }
}

/* ── helpers ───────────────────────────────────────────────────────────────────── */

function applyClaims(actions: Action[], claims: ScriptClaim[]): Action[] {
  if (!claims.length) return actions;
  const compiled = claims.map((c) => ({ ...c, re: safeRe(c.match) })).filter((c) => c.re);
  for (const action of actions) {
    for (const c of compiled) {
      if (c.re!.test(action.id)) {
        action.group = c.group;
        if (c.labelPrefix && !action.label.startsWith(c.labelPrefix)) action.label = c.labelPrefix + action.label;
        if (c.risk) action.risk = c.risk;
        break;
      }
    }
  }
  return actions;
}

function buildAction(
  pluginId: string,
  group: string,
  spec: SectionContribution["actions"][number],
  byId: Map<string, Action>,
  warnings: Warning[],
): Action | undefined {
  const base = {
    id: spec.id,
    label: spec.label,
    description: spec.description,
    group,
    cwd: ".",
    risk: spec.risk ?? "safe",
    longRunning: spec.longRunning ?? false,
    confidence: "explicit" as const,
    why: `plugin: ${pluginId}`,
    evidence: [],
  };
  if (spec.internal) {
    return { ...base, launcher: "", argv: [], source: `plugin:internal:${pluginId}:${spec.internal}` };
  }
  if (spec.delegateTo) {
    const target = byId.get(spec.delegateTo);
    if (!target) {
      warnings.push({ code: "plugin_delegate_missing", detail: `${pluginId}: no script "${spec.delegateTo}"` });
      return undefined;
    }
    return {
      ...base,
      launcher: target.launcher,
      argv: target.argv,
      cwd: target.cwd,
      longRunning: spec.longRunning ?? target.longRunning,
      risk: spec.risk ?? target.risk,
      source: `plugin:${pluginId}`,
    };
  }
  if (spec.cmd) {
    const tokens = tokenize(spec.cmd);
    if (!tokens.length) return undefined;
    return { ...base, launcher: tokens[0]!, argv: tokens.slice(1), source: `plugin:${pluginId}` };
  }
  warnings.push({ code: "plugin_action_empty", detail: `${pluginId}: action "${spec.id}" has no cmd/delegateTo/internal` });
  return undefined;
}

function safeDetect(plugin: MenuPlugin, c: PluginContext): boolean {
  try {
    return plugin.detect ? plugin.detect(c) === true : false;
  } catch {
    return false;
  }
}

function safeContribute(plugin: MenuPlugin, c: PluginContext, warnings: Warning[]): PluginContribution {
  try {
    return plugin.contribute(c) ?? {};
  } catch (e) {
    warnings.push({ code: "plugin_error", detail: `${plugin.id}: ${(e as Error).message}` });
    return {};
  }
}

function readDeps(root: string): Record<string, string> {
  const pkg = readJson<{
    dependencies?: Record<string, string>;
    devDependencies?: Record<string, string>;
    optionalDependencies?: Record<string, string>;
    peerDependencies?: Record<string, string>;
  }>(join(root, "package.json"));
  return {
    ...(pkg?.dependencies ?? {}),
    ...(pkg?.devDependencies ?? {}),
    ...(pkg?.optionalDependencies ?? {}),
    ...(pkg?.peerDependencies ?? {}),
  };
}

function clean<T extends object>(obj: T): Partial<T> {
  const out: Partial<T> = {};
  for (const [k, v] of Object.entries(obj)) if (v !== undefined && v !== "") (out as Record<string, unknown>)[k] = v;
  return out;
}

function cacheFile(name: string): string {
  return /\.json$/i.test(name) ? name : `${name}.json`;
}

function safeRe(source: string): RegExp | undefined {
  try {
    return new RegExp(source);
  } catch {
    return undefined;
  }
}
