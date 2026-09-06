import type { Theme } from "./theme.js";
import type { MenuModel } from "../builder/build.js";
import type { GitInfo } from "../util/git.js";
import type { Settings } from "./settings.js";
import type { Endpoint } from "../manifest/schema.js";
import type { PluginTone } from "../plugins/types.js";

/** Friendly display names for stack chips. */
export const FRAMEWORK_LABELS: Record<string, string> = {
  // Web meta-frameworks
  next: "Next.js",
  nuxt: "Nuxt",
  astro: "Astro",
  remix: "Remix",
  sveltekit: "SvelteKit",
  gatsby: "Gatsby",
  angular: "Angular",
  redwood: "RedwoodJS",
  docusaurus: "Docusaurus",
  vitepress: "VitePress",
  svelte: "Svelte",
  solid: "Solid",
  qwik: "Qwik",
  preact: "Preact",
  vue: "Vue",
  vite: "Vite",
  react: "React",
  // Backend / API
  nest: "NestJS",
  fastify: "Fastify",
  koa: "Koa",
  adonis: "AdonisJS",
  elysia: "Elysia",
  express: "Express",
  hono: "Hono",
  // Desktop / mobile shells
  tauri: "Tauri",
  electron: "Electron",
  neutralino: "Neutralino",
  capacitor: "Capacitor",
  expo: "Expo",
  "react-native": "React Native",
  // Apple / native platforms
  apple: "Apple / Xcode",
  swift: "Swift",
  android: "Android",
  flutter: "Flutter",
  wails: "Wails",
  // Languages / runtimes / build systems
  python: "Python",
  rust: "Rust",
  go: "Go",
  ruby: "Ruby",
  elixir: "Elixir",
  java: "Java / Gradle",
  cmake: "CMake",
  zig: "Zig",
  deno: "Deno",
  bun: "Bun",
  laravel: "Laravel",
  php: "PHP",
  dotnet: ".NET",
  // Game engines
  unity: "Unity",
  godot: "Godot",
  unreal: "Unreal",
  // Monorepo orchestrators
  turborepo: "Turborepo",
  nx: "Nx",
};

export interface CosmicContext {
  theme: Theme;
  git: GitInfo;
  mode: string;
  settings: Settings;
  toolVersion: string;
}

/**
 * The cosmic banner — schema's `dev-banner.mjs` look (open box, accent rules, glyph rows),
 * but built from detection so any project gets it. Each row renders ONLY when its data exists
 * AND the matching banner toggle is on. Rows are grouped; an empty group drops its divider too,
 * so the box never shows a hanging rule.
 */
export function renderCosmicBanner(model: MenuModel, ctx: CosmicContext, width: number): string[] {
  if (ctx.settings.bannerStyle === "console") return renderConsoleBanner(model, ctx, width);
  const { theme, settings } = ctx;
  const { c, accent } = theme;
  const inner = Math.max(20, width);
  const hChar = theme.unicode ? "─" : "-";
  const vChar = theme.unicode ? "│" : "|";

  const rule = () => "  " + accent(hChar.repeat(inner));
  const row = (text: string) => "  " + accent(vChar) + "  " + clip(text, inner - 3);

  const groups: string[][] = [];

  // ── Identity ───────────────────────────────────────────────────────────────
  const identity: string[] = [];
  const name = model.textOverrides?.title ?? model.title;
  const titleText = settings.banner.spacedTitle ? spaced(name) : name.toUpperCase();
  const tagline = model.textOverrides?.tagline ?? settings.banner.tagline ?? "developer console";
  const purpose = model.textOverrides?.purpose ?? model.purpose;
  const modeTag = ctx.mode && ctx.mode !== "local" ? `  ${c.dim("·")}  ${c.yellow(ctx.mode)}` : "";
  identity.push(
    `${icon(theme, "🌌", "◆", "*")}  ${c.bold(accent(titleText))}  ${c.dim("·")}  ${c.dim(tagline)}${modeTag}`,
  );
  if (purpose) identity.push(c.dim(purpose));
  for (const phrase of model.bannerPhrases ?? []) identity.push(c.dim(phrase));
  groups.push(identity);

  // ── Endpoints (🔭 web / ⚡ api) ──────────────────────────────────────────────
  if (settings.banner.endpoints && model.endpoints.length) {
    const col = Math.min(14, Math.max(8, ...model.endpoints.map((e) => e.label.length)));
    groups.push(model.endpoints.map((ep) => endpointRow(ep, theme, col)));
  }

  // ── Context: stack · branch · runtime · date ────────────────────────────────
  const context: string[] = [];
  if (settings.banner.stack) {
    const chips = stackChips(model);
    if (chips.length) {
      context.push(
        `${icon(theme, "🧩", "◆", "#")}  ${label("stack", c)}  ${chips.map((ch) => accent(ch)).join(c.dim("  ·  "))}`,
      );
    }
  }
  if (settings.banner.git && ctx.git.isRepo && ctx.git.branch) {
    const sha = ctx.git.sha ? `  ${c.dim("@" + ctx.git.sha)}` : "";
    const dirty = ctx.git.dirty ? c.yellow(`  ·  ${ctx.git.dirty} changed`) : c.green("  ·  clean");
    context.push(`${icon(theme, "🌿", "⎇", "~")}  ${label("branch", c)}  ${c.green(ctx.git.branch)}${sha}${dirty}`);
  }
  if (settings.banner.runtime) {
    context.push(
      `${icon(theme, "🖥", "▢", "=")}  ${label("node", c)}  ${c.white(process.version)}  ${c.dim("·")}  ${c.dim(`${process.platform} ${process.arch}`)}`,
    );
  }
  if (settings.banner.date) {
    context.push(`${icon(theme, "🌍", "◷", "@")}  ${label("date", c)}  ${c.white(formatDate())}`);
  }
  if (context.length) groups.push(context);

  // ── Scale: packages · counts ────────────────────────────────────────────────
  const scale: string[] = [];
  if (settings.banner.packages && model.packages.length) {
    scale.push(`${icon(theme, "🪐", "◌", "*")}  ${label("packages", c)}  ${c.white(packageList(model.packages))}`);
  }
  if (settings.banner.stats) {
    const parts = statParts(model);
    if (parts.length) scale.push(`${icon(theme, "📊", "▦", "%")}  ${c.dim(parts.join(`  ${c.dim("·")}  `))}`);
  }
  if (scale.length) groups.push(scale);

  // ── Plugin banner lines (e.g. the store stat line) ──────────────────────────
  if (settings.banner.plugins && model.bannerLines?.length) {
    groups.push(
      model.bannerLines.map(
        (line) => `${icon(theme, "🔌", "◆", "+")}  ${pluginTone(theme, line.tone)(line.text)}`,
      ),
    );
  }

  // ── Warnings (only if any) ──────────────────────────────────────────────────
  const warns: string[] = [];
  if (model.defError) warns.push(c.red(`${theme.sym.warning} ${model.defError}`));
  for (const w of model.warnings) warns.push(c.yellow(`${theme.sym.warning} ${w.code}: ${w.detail}`));
  if (warns.length) groups.push(warns);

  // Stitch present groups together with dividers.
  const lines: string[] = [rule()];
  groups.forEach((group, i) => {
    for (const line of group) lines.push(row(line));
    if (i < groups.length - 1) lines.push(rule());
  });
  lines.push(rule());
  return lines;
}

/**
 * The console banner style — a compact closed box (title + subtitle) over a single status line
 * (`⎇ branch · N changed · license: X · N commands`). Mirrors the navig workspace console.
 */
function renderConsoleBanner(model: MenuModel, ctx: CosmicContext, width: number): string[] {
  const { theme, settings } = ctx;
  const { c, accent } = theme;
  const inner = Math.max(20, width);
  const h = theme.unicode ? "─" : "-";
  const v = theme.unicode ? "│" : "|";
  const innerW = inner - 3; // after "│ "
  const sparkle = icon(theme, "✦", "✦", "*");
  const title = model.textOverrides?.title ?? model.title;
  const version = model.version ? c.dim(`v${model.version}`) : "";
  const subtitle = model.textOverrides?.purpose ?? model.purpose ?? model.textOverrides?.tagline ?? settings.banner.tagline;

  // Open box (left rail only) — robust against ambiguous glyph widths; the version floats right.
  const rule = "  " + accent(h.repeat(inner));
  const row = (text: string) => "  " + accent(v) + " " + clip(text, innerW);
  const titleLeft = `${accent(sparkle)} ${c.bold(accent(title))}`;
  const gap = innerW - dw(strip(titleLeft)) - dw(strip(version));
  const titleRow = version && gap >= 1 ? titleLeft + " ".repeat(gap) + version : titleLeft;

  const lines = [rule, row(titleRow)];
  if (subtitle) lines.push(row(c.dim(subtitle)));
  lines.push(rule);
  lines.push("");

  // Status line: branch · changed · license · commands.
  const parts: string[] = [];
  if (settings.banner.git && ctx.git.isRepo && ctx.git.branch) {
    parts.push(`${icon(theme, "🌿", "⎇", "@")} ${c.green(ctx.git.branch)}`);
    parts.push(ctx.git.dirty ? c.yellow(`${ctx.git.dirty} changed`) : c.green("clean"));
  }
  if (model.license && !/^(unlicensed|private)$/i.test(model.license)) {
    parts.push(`${c.dim("license:")} ${c.white(model.license)}`);
  }
  parts.push(`${c.white(String(model.allScripts.length))} ${c.dim("commands")}`);
  if (model.stats.docs) parts.push(`${c.white(String(model.stats.docs))} ${c.dim("docs")}`);
  if (parts.length) lines.push("  " + clip(parts.join(c.dim("  ·  ")), inner));

  if (settings.banner.plugins && model.bannerLines?.length) {
    for (const line of model.bannerLines) lines.push("  " + `${icon(theme, "🔌", "◆", "+")}  ${pluginTone(theme, line.tone)(line.text)}`);
  }
  if (model.defError) lines.push("  " + c.red(`${theme.sym.warning} ${model.defError}`));
  for (const w of model.warnings) lines.push("  " + c.yellow(`${theme.sym.warning} ${w.code}: ${w.detail}`));
  return lines;
}

function endpointRow(ep: Endpoint, theme: Theme, col: number): string {
  const { c, accent } = theme;
  const ico =
    ep.kind === "api"
      ? icon(theme, "⚡", "▸", ">")
      : ep.kind === "service"
        ? icon(theme, "🛰", "◇", "+")
        : icon(theme, "🔭", "◉", "o");
  const tint = ep.kind === "api" ? c.blueBright : accent;
  const badge = ep.tls ? `   ${c.green("TLS ✓")}` : "";
  return `${ico}  ${c.bold(tint(ep.label.padEnd(col)))}  ${c.white(ep.url)}${badge}`;
}

function stackChips(model: MenuModel): string[] {
  const chips: string[] = [];
  if (model.packageManager && model.packageManager !== "none") chips.push(model.packageManager);
  for (const f of model.frameworks) chips.push(FRAMEWORK_LABELS[f] ?? f);
  for (const s of model.services) chips.push(capitalize(s.id));
  return chips.slice(0, 8);
}

function statParts(model: MenuModel): string[] {
  const parts: string[] = [];
  if (model.stats.scripts) parts.push(`${model.stats.scripts} scripts`);
  if (model.stats.packages) parts.push(`${model.stats.packages} packages`);
  if (model.stats.docs) parts.push(`${model.stats.docs} docs`);
  if (model.stats.services) parts.push(`${model.stats.services} services`);
  return parts;
}

function packageList(packages: string[]): string {
  const names = packages.map((p) => p.split("/").filter(Boolean).pop() ?? p);
  const shown = names.slice(0, 5).join("  ·  ");
  return names.length > 5 ? `${shown}  (+${names.length - 5})` : shown;
}

function label(text: string, c: Theme["c"], width = 8): string {
  return c.dim(text.padEnd(width));
}

function icon(theme: Theme, emoji: string, unicode: string, ascii: string): string {
  return theme.emoji ? emoji : theme.unicode ? unicode : ascii;
}

function pluginTone(theme: Theme, tone?: PluginTone): (s: string) => string {
  const { c, accent } = theme;
  switch (tone) {
    case "green":
      return c.green;
    case "yellow":
      return c.yellow;
    case "red":
      return c.red;
    case "blue":
      return c.blueBright;
    case "cyan":
      return c.cyanBright;
    case "magenta":
      return c.magentaBright;
    case "purple":
      return c.magenta;
    case "white":
      return c.white;
    case "dim":
      return c.dim;
    default:
      return accent;
  }
}

function spaced(name: string): string {
  const upper = name.toUpperCase();
  return upper.length <= 14 ? upper.split("").join(" ") : upper;
}

function formatDate(): string {
  try {
    return new Date().toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" });
  } catch {
    return new Date().toISOString().slice(0, 16).replace("T", " ");
  }
}

function capitalize(s: string): string {
  return s ? s[0]!.toUpperCase() + s.slice(1) : s;
}

function strip(text: string): string {
  return text.replace(/\x1b\[[0-9;]*m/g, "");
}

/** Display width (emoji / wide glyphs count as 2), used for the closed console box. */
function dw(text: string): number {
  let w = 0;
  for (const ch of strip(text)) {
    const cp = ch.codePointAt(0)!;
    if (cp === 0xfe0f || cp === 0x200d || (cp >= 0x300 && cp <= 0x36f)) continue;
    const wide = (cp >= 0x1100 && cp <= 0x115f) || (cp >= 0x2e80 && cp <= 0xa4cf) || (cp >= 0xac00 && cp <= 0xd7a3) ||
      (cp >= 0xf900 && cp <= 0xfaff) || (cp >= 0xff00 && cp <= 0xff60) || (cp >= 0x1f000 && cp <= 0x1faff) || (cp >= 0x2600 && cp <= 0x27bf);
    w += wide ? 2 : 1;
  }
  return w;
}

function padRight(text: string, width: number): string {
  const len = dw(strip(text));
  return len >= width ? text : text + " ".repeat(width - len);
}

/* width-aware clip (kept local so the banner is self-contained). */
function clip(text: string, width: number): string {
  const target = Math.max(1, width);
  let out = "";
  let visible = 0;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]!;
    if (ch === "\x1b") {
      const match = /\x1b\[[0-9;]*m/.exec(text.slice(i));
      if (match?.index === 0) {
        out += match[0];
        i += match[0].length - 1;
        continue;
      }
    }
    if (visible >= target - 1) return out + "…";
    out += ch;
    visible++;
  }
  return out;
}
