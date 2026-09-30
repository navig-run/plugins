import type { CanonicalAction } from "../config/constants.js";

/** Menu groups, in display order. */
export const GROUPS = [
  "Development",
  "Quality",
  "Database",
  "Build",
  "Deploy",
  "Desktop & Store",
  "i18n",
  "Assets",
  "Operations",
  "Misc",
] as const;
export type Group = (typeof GROUPS)[number];

/** Accent tone for a section header (resolved against the theme's chalk instance in the UI). */
export type ToneKey = "blue" | "yellow" | "cyan" | "green" | "red" | "magenta" | "purple" | "accent";

export interface SectionMeta {
  title: string;
  emoji: string;
  unicode: string;
  ascii: string;
  tone: ToneKey;
}

/**
 * Presentation for every section the menu can render — real groups plus the synthetic
 * "Recent" and "NAVIG" rails. One source of truth for icon + colour so the list reads like
 * schema's bespoke console regardless of which project we point at.
 */
export const SECTION_META: Record<string, SectionMeta> = {
  Recent: { title: "RECENT", emoji: "↻", unicode: "↻", ascii: "~", tone: "purple" },
  Development: { title: "DEVELOPMENT", emoji: "⚡", unicode: "▸", ascii: ">", tone: "blue" },
  Quality: { title: "QUALITY", emoji: "🧪", unicode: "◆", ascii: "*", tone: "yellow" },
  Database: { title: "DATABASE", emoji: "🗄", unicode: "▤", ascii: "#", tone: "cyan" },
  Build: { title: "BUILD", emoji: "📦", unicode: "▣", ascii: "=", tone: "green" },
  Deploy: { title: "DEPLOY", emoji: "🚀", unicode: "▲", ascii: "^", tone: "red" },
  "Desktop & Store": { title: "DESKTOP / STORE", emoji: "🖥", unicode: "▦", ascii: "@", tone: "magenta" },
  i18n: { title: "I18N", emoji: "🌐", unicode: "◍", ascii: "%", tone: "cyan" },
  Assets: { title: "ASSETS", emoji: "🎨", unicode: "◨", ascii: "&", tone: "magenta" },
  Operations: { title: "OPERATIONS", emoji: "🛠", unicode: "⚙", ascii: "+", tone: "magenta" },
  Misc: { title: "MISC", emoji: "🧩", unicode: "◆", ascii: "-", tone: "accent" },
  // The synthetic bottom rail (settings / customizer / regenerate / refresh / doctor).
  Utilities: { title: "UTILITIES", emoji: "🧰", unicode: "◆", ascii: "$", tone: "accent" },
};

const FALLBACK_META: SectionMeta = SECTION_META.Misc!;

/** Plugin-contributed section presentation, kept separate so we never mutate the static table. */
const RUNTIME_META: Record<string, SectionMeta> = {};

/** Register presentation for a plugin section group. Core groups are never clobbered. */
export function registerSectionMeta(
  group: string,
  meta: { title?: string; emoji?: string; unicode?: string; ascii?: string; tone?: string },
): void {
  if (SECTION_META[group]) return;
  const emoji = meta.emoji ?? "◆";
  RUNTIME_META[group] = {
    title: meta.title ?? group.toUpperCase(),
    emoji,
    unicode: meta.unicode ?? (isAscii(emoji) ? emoji : "◆"),
    ascii: meta.ascii ?? "*",
    tone: coerceTone(meta.tone),
  };
}

const TONES: ToneKey[] = ["blue", "yellow", "cyan", "green", "red", "magenta", "purple", "accent"];
function coerceTone(tone: string | undefined): ToneKey {
  return tone && (TONES as string[]).includes(tone) ? (tone as ToneKey) : "accent";
}

function isAscii(s: string): boolean {
  return /^[\x20-\x7e]+$/.test(s);
}

/** Resolve a section's presentation by its (case-insensitive) group key, with a sane fallback. */
export function sectionMeta(group: string): SectionMeta {
  if (SECTION_META[group]) return SECTION_META[group]!;
  if (RUNTIME_META[group]) return RUNTIME_META[group]!;
  const hit = Object.values(SECTION_META).find((m) => m.title.toLowerCase() === group.toLowerCase());
  return hit ?? { ...FALLBACK_META, title: group.toUpperCase() };
}

const CANONICAL_GROUP: Partial<Record<CanonicalAction, Group>> = {
  dev: "Development",
  start: "Development",
  preview: "Development",
  test: "Quality",
  typecheck: "Quality",
  lint: "Quality",
  "lint:fix": "Quality",
  build: "Build",
  deploy: "Deploy",
  migrate: "Database",
  seed: "Database",
  clean: "Operations",
};

/**
 * Token rules, most-specific first. A script name is split on `:`/`-`/`.`/`/` and EVERY token is
 * tested, so namespaced scripts land in the right rail — `api:migration:run` → Database,
 * `webapp:dev` → Development, `desktop:typecheck` → Desktop & Store, `i18n:extract` → i18n.
 */
const TOKEN_RULES: Array<[Group, RegExp]> = [
  ["i18n", /^(i18n|intl|locale|locales|translation|translations|translate|lang)$/],
  ["Desktop & Store", /^(tauri|electron|desktop|store|steam|msix|appx|msixupload|appxupload|appimage|dmg|installer|makeappx|wack|sign|signtool|notarize|codesign|updater|sideload)$/],
  ["Assets", /^(icons?|assets?|capture|screenshot|screenshots|thumbnail|thumbnails|themes?|svg|art|media|logo|logos|branding)$/],
  ["Database", /^(db|database|migration|migrations|migrate|prisma|drizzle|typeorm|sql|sqlite|seed|seeds|zone|studio|postgres|mysql|mongo)$/],
  ["Deploy", /^(deploy|release|publish|ship|rollout|wrangler|cloudflare|cf|vercel|netlify|docker|compose|pm2|vps|provision|infra|tunnel)$/],
  ["Build", /^(build|bundle|compile|dist|tsup|rollup|esbuild|webpack|package|pack)$/],
  ["Quality", /^(test|tests|lint|format|fmt|prettier|typecheck|tsc|check|coverage|cov|e2e|playwright|cypress|vitest|jest|audit|ci|smoke|spec|bench|benchmark|perf)$/],
  ["Development", /^(dev|develop|serve|start|watch|storybook|sb|hmr|preview|up)$/],
  ["Operations", /^(clean|reset|nuke|setup|install|bootstrap|init|env|doctor|sync|kill|stop|status|update|upgrade|prepare|generate|gen|codegen|scaffold|create|version|bump|preflight|validate|config|configs|report|pipeline|orchestrate|changeset|pull|clone|fetch|push|commit|checkout)$/],
];

function tokenize(name: string): string[] {
  return name.toLowerCase().split(/[:_\-./\s]+/).filter(Boolean);
}

export function groupFor(name: string, canonical?: CanonicalAction): Group {
  if (canonical && CANONICAL_GROUP[canonical]) return CANONICAL_GROUP[canonical]!;
  const tokens = tokenize(name);
  for (const [group, re] of TOKEN_RULES) {
    if (tokens.some((t) => re.test(t))) return group;
  }
  return "Misc";
}

/** Long-running launchers stream output and need clean Ctrl+C handling (dev servers, watchers). */
export function isLongRunning(name: string, command: string, canonical?: string): boolean {
  if (canonical === "dev" || canonical === "start" || canonical === "preview") return true;
  return /(--watch\b|\bwatch\b|\bdev\b|\bserve\b|nodemon|storybook|vite(?!\s+build))/i.test(
    `${name} ${command}`,
  );
}
