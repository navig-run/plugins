import type { Action, Manifest, Risk, Warning } from "../manifest/schema.js";
import type { Theme } from "../ui/theme.js";

/**
 * The plugin API for `navig-menu`. A plugin can change the banner (lines/phrases), overwrite text
 * (title/tagline/prompt), contribute sections + commands, claim/relabel detected scripts, add
 * settings, and expose internal actions with handlers. Two tiers share this interface:
 *   - **declarative** — a JSON/YAML manifest (pure data, safe anywhere), mapped by `manifest.ts`.
 *   - **programmatic** — a module exporting `definePlugin({...})` (adds `handlers` for dynamic work).
 *
 * Plugins run ONLY in the model/menu phase — never during the pure detection scan. See AGENTS.md.
 */

export type PluginTone =
  | "accent" | "dim" | "green" | "yellow" | "red" | "blue" | "cyan" | "magenta" | "purple" | "white";

export interface BannerLine {
  text: string;
  tone?: PluginTone;
}

export interface TextOverrides {
  title?: string;
  tagline?: string;
  purpose?: string;
  prompt?: string;
}

export interface PluginActionSpec {
  id: string;
  label: string;
  cmd?: string;
  description?: string;
  risk?: Risk;
  longRunning?: boolean;
  /** Route to a detected script id (its launcher/argv are copied). */
  delegateTo?: string;
  /** Handler key (programmatic plugins only) — runs `handlers[internal]`. */
  internal?: string;
}

export interface SectionMetaSpec {
  title?: string;
  emoji?: string;
  unicode?: string;
  ascii?: string;
  tone?: string;
}

export interface SectionContribution {
  group: string;
  meta?: SectionMetaSpec;
  actions: PluginActionSpec[];
}

export interface ScriptClaim {
  /** Regex source, tested against `action.id`. */
  match: string;
  group: string;
  labelPrefix?: string;
  risk?: Risk;
}

export interface PluginSettingSpec {
  key: string;
  label: string;
  type: "toggle" | "cycle";
  values?: string[];
  default: string | boolean;
}

export interface PluginContribution {
  text?: TextOverrides;
  bannerLines?: BannerLine[];
  bannerPhrases?: string[];
  sections?: SectionContribution[];
  claims?: ScriptClaim[];
  settings?: PluginSettingSpec[];
  about?: string[];
}

/** Read-only context for `detect` / `contribute` — no spawn, no writes. */
export interface PluginContext {
  root: string;
  manifest: Manifest;
  scripts: Record<string, string>;
  /** True if `<root>/<rel>` exists (best-effort file read). */
  hasFile(rel: string): boolean;
  /** True if any dependency name matches (exact, or `@scope/` prefix). */
  hasDep(...names: string[]): boolean;
  /** Resolved settings for THIS plugin (defaults merged with .navig/menu.json). */
  settings: Record<string, string | boolean>;
  /** Directory for this plugin's caches (gitignored). */
  cacheDir: string;
  readCache<T = unknown>(name: string): T | undefined;
}

/** Menu-phase context for handlers — sanctioned side effects only (argv exec, cache write, prompts). */
export interface PluginActionContext extends PluginContext {
  theme: Theme;
  /** Spawn a command as an argv array (no shell). Returns the exit code. */
  run(launcher: string, argv: string[], cwd?: string): Promise<number>;
  /** Run a command as an argv array (no shell) and capture its stdout. */
  capture(launcher: string, argv: string[], cwd?: string): Promise<{ stdout: string; exitCode: number }>;
  /** Interactive picker (returns the chosen `name`, or undefined if cancelled). */
  select(opts: { message: string; choices: { name: string; message?: string; hint?: string }[] }): Promise<string | undefined>;
  /** Free-text prompt (undefined if cancelled). */
  input(message: string): Promise<string | undefined>;
  /** Yes/no prompt. */
  confirm(message: string): Promise<boolean>;
  writeCache(name: string, data: unknown): void;
  /** Show a one-line message back in the menu footer. */
  notify(message: string): void;
}

export type PluginTier = "declarative" | "programmatic";
export type PluginOrigin = "builtin" | "npm" | "local";

export interface MenuPlugin {
  id: string;
  tier: PluginTier;
  version?: string;
  detect?(ctx: PluginContext): boolean;
  contribute(ctx: PluginContext): PluginContribution;
  handlers?: Record<string, (ctx: PluginActionContext) => Promise<void> | void>;
}

/** Author entry point — identity helper for type-checked plugins. */
export function definePlugin(plugin: MenuPlugin): MenuPlugin {
  return plugin;
}

/** A discovered plugin plus where it came from (and any load error). */
export interface LoadedPlugin {
  plugin: MenuPlugin;
  origin: PluginOrigin;
  error?: string;
}

/* ── apply results (folded into MenuModel) ─────────────────────────────────────── */

export interface ResolvedPluginSetting {
  pluginId: string;
  key: string;
  label: string;
  type: "toggle" | "cycle";
  values?: string[];
  value: string | boolean;
}

export interface PluginSummary {
  id: string;
  tier: PluginTier;
  origin: PluginOrigin;
  version?: string;
  active: boolean;
  /** Activated by `detect()` rather than an explicit enable. */
  auto: boolean;
  error?: string;
}

export interface PluginApply {
  actions: Action[];
  text: TextOverrides;
  bannerLines: BannerLine[];
  bannerPhrases: string[];
  about: string[];
  pluginSettings: ResolvedPluginSetting[];
  loaded: PluginSummary[];
  warnings: Warning[];
}
