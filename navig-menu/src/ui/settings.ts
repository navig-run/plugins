import { mkdirSync, writeFileSync } from "node:fs";
import { definitionPath, navigDir } from "../manifest/paths.js";
import { loadDefinition } from "../builder/build.js";
import { generateMenuDefinition } from "../builder/generate.js";
import type {
  Accent,
  GlyphStyle,
  Manifest,
  MenuDefinition,
  MenuUi,
} from "../manifest/schema.js";

export type GlyphSetting = GlyphStyle | "auto";

export interface BannerSettings {
  spacedTitle: boolean;
  tagline?: string;
  endpoints: boolean;
  stack: boolean;
  git: boolean;
  runtime: boolean;
  date: boolean;
  packages: boolean;
  stats: boolean;
  /** Plugin-contributed banner lines (e.g. the store stat line). */
  plugins: boolean;
}

/** Fully-resolved, concrete settings — project `.navig/menu.json` over built-in app defaults. */
export interface Settings {
  accent: Accent;
  glyphs: GlyphSetting;
  density: "comfortable" | "compact";
  layout: "flat" | "list" | "categories" | "tree" | "projects";
  bannerStyle: "cosmic" | "console";
  showDescriptions: boolean;
  showCommandHints: boolean;
  showCounts: boolean;
  showRiskBadges: boolean;
  showRecents: boolean;
  recentLimit: number;
  showNavigSection: boolean;
  /** Mouse-wheel scrolls the list (captures the mouse — Shift-drag still selects text). */
  mouse: boolean;
  banner: BannerSettings;
  footer?: string;
}

/** The app fallback — what every project gets before any customization. */
export const DEFAULT_SETTINGS: Settings = {
  accent: "cyan",
  glyphs: "ascii",
  density: "comfortable",
  layout: "flat",
  bannerStyle: "cosmic",
  showDescriptions: true,
  showCommandHints: true,
  showCounts: true,
  showRiskBadges: false,
  showRecents: true,
  recentLimit: 5,
  showNavigSection: true,
  mouse: true,
  banner: {
    spacedTitle: true,
    endpoints: true,
    stack: true,
    git: true,
    runtime: true,
    date: true,
    packages: true,
    stats: true,
    plugins: true,
  },
};

const ACCENTS: Accent[] = ["cyan", "blue", "green", "magenta", "purple", "yellow", "red"];

function isAccent(value: unknown): value is Accent {
  return typeof value === "string" && (ACCENTS as string[]).includes(value);
}

/** Merge a project's `ui` block (+ top-level accent) over the app defaults. Pure. */
export function resolveSettings(ui: MenuUi | undefined, accent?: string): Settings {
  const d = DEFAULT_SETTINGS;
  const b = ui?.banner ?? {};
  const resolvedAccent = isAccent(accent) ? accent : isAccent(ui?.accent) ? ui!.accent! : d.accent;
  return {
    accent: resolvedAccent,
    glyphs: ui?.glyphs ?? d.glyphs,
    density: ui?.density ?? d.density,
    layout: ui?.layout ?? d.layout,
    bannerStyle: ui?.bannerStyle ?? d.bannerStyle,
    showDescriptions: ui?.showDescriptions ?? d.showDescriptions,
    showCommandHints: ui?.showCommandHints ?? d.showCommandHints,
    showCounts: ui?.showCounts ?? d.showCounts,
    showRiskBadges: ui?.showRiskBadges ?? d.showRiskBadges,
    showRecents: ui?.showRecents ?? d.showRecents,
    recentLimit: ui?.recentLimit ?? d.recentLimit,
    showNavigSection: ui?.showNavigSection ?? d.showNavigSection,
    mouse: ui?.mouse ?? d.mouse,
    banner: {
      spacedTitle: b.spacedTitle ?? d.banner.spacedTitle,
      tagline: b.tagline ?? d.banner.tagline,
      endpoints: b.endpoints ?? d.banner.endpoints,
      stack: b.stack ?? d.banner.stack,
      git: b.git ?? d.banner.git,
      runtime: b.runtime ?? d.banner.runtime,
      date: b.date ?? d.banner.date,
      packages: b.packages ?? d.banner.packages,
      stats: b.stats ?? d.banner.stats,
      plugins: b.plugins ?? d.banner.plugins,
    },
    footer: ui?.footer ?? d.footer,
  };
}

/** Serialize resolved settings back into a `ui` block written to `.navig/menu.json`. */
export function settingsToUi(s: Settings): MenuUi {
  const ui: MenuUi = {
    recentLimit: s.recentLimit,
    showDescriptions: s.showDescriptions,
    showCommandHints: s.showCommandHints,
    showCounts: s.showCounts,
    showRiskBadges: s.showRiskBadges,
    showRecents: s.showRecents,
    showNavigSection: s.showNavigSection,
    mouse: s.mouse,
    density: s.density,
    layout: s.layout,
    bannerStyle: s.bannerStyle,
    accent: s.accent,
    banner: {
      spacedTitle: s.banner.spacedTitle,
      endpoints: s.banner.endpoints,
      stack: s.banner.stack,
      git: s.banner.git,
      runtime: s.banner.runtime,
      date: s.banner.date,
      packages: s.banner.packages,
      stats: s.banner.stats,
      plugins: s.banner.plugins,
    },
  };
  if (s.glyphs !== "auto") ui.glyphs = s.glyphs;
  if (s.banner.tagline) ui.banner!.tagline = s.banner.tagline;
  if (s.footer) ui.footer = s.footer;
  return ui;
}

/**
 * Persist settings to the project's `.navig/menu.json`, preserving every other field. If no
 * definition exists yet we generate a full one first (so customizing also captures the detected
 * actions) — keeping everything self-contained and regenerable.
 */
export function saveSettings(root: string, settings: Settings, manifest?: Manifest): MenuDefinition {
  const existing = loadDefinition(root).def;
  const base: MenuDefinition =
    existing ?? (manifest ? generateMenuDefinition(manifest).definition : {});
  const def: MenuDefinition = { ...base, ui: settingsToUi(settings), accent: settings.accent };
  mkdirSync(navigDir(root), { recursive: true });
  writeFileSync(definitionPath(root), JSON.stringify(def, null, 2) + "\n", "utf8");
  return def;
}

/**
 * Persist the hidden-items list (the menu customizer) to `.navig/menu.json`, preserving everything
 * else. A category with all of its items hidden simply renders empty and drops out of the menu.
 */
export function saveHidden(root: string, hide: string[], manifest?: Manifest): MenuDefinition {
  const existing = loadDefinition(root).def;
  const base: MenuDefinition =
    existing ?? (manifest ? generateMenuDefinition(manifest).definition : {});
  const def: MenuDefinition = { ...base, hide: hide.length ? [...new Set(hide)] : undefined };
  mkdirSync(navigDir(root), { recursive: true });
  writeFileSync(definitionPath(root), JSON.stringify(def, null, 2) + "\n", "utf8");
  return def;
}

/**
 * Persist a personal pre-run note for one action (`overrides[id].note`), preserving everything
 * else. A blank note clears it (and drops an otherwise-empty override entry). Mirrors {@link saveHidden}.
 */
export function saveNote(
  root: string,
  id: string,
  note: string | undefined,
  manifest?: Manifest,
): MenuDefinition {
  const existing = loadDefinition(root).def;
  const base: MenuDefinition =
    existing ?? (manifest ? generateMenuDefinition(manifest).definition : {});
  const overrides = { ...(base.overrides ?? {}) };
  const prev = { ...(overrides[id] ?? {}) };
  const trimmed = (note ?? "").trim();
  if (trimmed) {
    overrides[id] = { ...prev, note: trimmed };
  } else {
    delete prev.note;
    if (Object.keys(prev).length) overrides[id] = prev;
    else delete overrides[id];
  }
  const def: MenuDefinition = {
    ...base,
    overrides: Object.keys(overrides).length ? overrides : undefined,
  };
  mkdirSync(navigDir(root), { recursive: true });
  writeFileSync(definitionPath(root), JSON.stringify(def, null, 2) + "\n", "utf8");
  return def;
}

/**
 * Persist a plugin's activation / setting state to `.navig/menu.json` (the `plugins` map),
 * preserving everything else. Mirrors {@link saveHidden}. `enabled` toggles activation; `settings`
 * deep-merges the plugin's per-key values.
 */
export function savePluginState(
  root: string,
  id: string,
  patch: { enabled?: boolean; settings?: Record<string, boolean | string> },
  manifest?: Manifest,
): MenuDefinition {
  const existing = loadDefinition(root).def;
  const base: MenuDefinition =
    existing ?? (manifest ? generateMenuDefinition(manifest).definition : {});
  const plugins = { ...(base.plugins ?? {}) };
  const prev = plugins[id] ?? {};
  plugins[id] = {
    ...prev,
    ...(patch.enabled !== undefined ? { enabled: patch.enabled } : {}),
    ...(patch.settings ? { settings: { ...(prev.settings ?? {}), ...patch.settings } } : {}),
  };
  const def: MenuDefinition = { ...base, plugins };
  mkdirSync(navigDir(root), { recursive: true });
  writeFileSync(definitionPath(root), JSON.stringify(def, null, 2) + "\n", "utf8");
  return def;
}
