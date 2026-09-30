import type { DetectContext } from "./context.js";
import { hasDep } from "./context.js";
import type { Framework, Evidence } from "../manifest/schema.js";

interface Rule {
  id: string;
  deps?: string[];
  files?: RegExp[];
  /** Strong signal (config file present) → detected; dep-only → detected; weak → inferred. */
}

const RULES: Rule[] = [
  // Web meta-frameworks (config file is the strong signal).
  { id: "next", deps: ["next"], files: [/^next\.config\.[mc]?[jt]s$/] },
  { id: "nuxt", deps: ["nuxt"], files: [/^nuxt\.config\.[mc]?[jt]s$/] },
  { id: "astro", deps: ["astro"], files: [/^astro\.config\.[mc]?[jt]s$/] },
  { id: "remix", deps: ["@remix-run/", "@react-router/"], files: [/^remix\.config\.[mc]?[jt]s$/] },
  { id: "sveltekit", deps: ["@sveltejs/kit"], files: [/^svelte\.config\.[mc]?[jt]s$/] },
  { id: "gatsby", deps: ["gatsby"], files: [/^gatsby-config\.[mc]?[jt]s$/] },
  { id: "angular", deps: ["@angular/core"], files: [/^angular\.json$/] },
  { id: "redwood", deps: ["@redwoodjs/core", "@redwoodjs/"] },
  { id: "docusaurus", deps: ["@docusaurus/core"] },
  { id: "vitepress", deps: ["vitepress"] },
  { id: "svelte", deps: ["svelte"] },
  { id: "solid", deps: ["solid-js", "@solidjs/start", "solid-start"] },
  { id: "qwik", deps: ["@builder.io/qwik"] },
  { id: "preact", deps: ["preact"] },
  { id: "vue", deps: ["vue"] },
  { id: "vite", deps: ["vite"], files: [/^vite\.config\.[mc]?[jt]s$/] },
  { id: "react", deps: ["react"] },
  // Backend / API frameworks.
  { id: "nest", deps: ["@nestjs/core"], files: [/^nest-cli\.json$/] },
  { id: "fastify", deps: ["fastify"] },
  { id: "koa", deps: ["koa"] },
  { id: "adonis", deps: ["@adonisjs/core"] },
  { id: "elysia", deps: ["elysia"] },
  { id: "express", deps: ["express"] },
  { id: "hono", deps: ["hono"] },
  // Desktop / mobile / cross-platform shells.
  { id: "tauri", deps: ["@tauri-apps/api", "@tauri-apps/cli"], files: [/^tauri(\..*)?\.conf\.json$/] },
  { id: "electron", deps: ["electron"], files: [/^electron-builder\.(json|ya?ml|[jt]s)$/] },
  { id: "neutralino", deps: ["@neutralinojs/neu"], files: [/^neutralino\.config\.json$/] },
  { id: "capacitor", deps: ["@capacitor/core"], files: [/^capacitor\.config\.(ts|js|json)$/] },
  { id: "expo", deps: ["expo"], files: [/^app\.(json|config\.[jt]s)$/] },
  { id: "react-native", deps: ["react-native"] },
];

export function detectFrameworks(ctx: DetectContext): Framework[] {
  const out: Framework[] = [];
  const baseNames = new Set(ctx.scan.files.map((f) => f.rel.split("/").pop() ?? ""));

  for (const rule of RULES) {
    const evidence: Evidence[] = [];
    if (rule.deps && hasDep(ctx, ...rule.deps)) {
      const hit = rule.deps.find((d) =>
        d.endsWith("/") ? Object.keys(ctx.deps).some((k) => k.startsWith(d)) : ctx.deps[d],
      );
      evidence.push({ kind: "dep", detail: hit });
    }
    if (rule.files) {
      for (const re of rule.files) {
        const f = [...baseNames].find((b) => re.test(b));
        if (f) evidence.push({ kind: "config", file: f });
      }
    }
    if (evidence.length) {
      const strong = evidence.some((e) => e.kind === "config");
      out.push({ id: rule.id, confidence: strong ? "detected" : "detected", evidence });
    }
  }

  // Apple / native platforms (nested manifests are reachable at the default scan depth).
  pushIf(out, ctx, "apple", [/\.xcworkspace\//, /\.xcodeproj\//, /\.pbxproj$/, /^Podfile$/, /^Info\.plist$/]);
  pushIf(out, ctx, "swift", [/^Package\.swift$/]);
  pushIf(out, ctx, "android", [/AndroidManifest\.xml$/]);
  pushIf(out, ctx, "flutter", [/^pubspec\.yaml$/]);
  pushIf(out, ctx, "wails", [/^wails\.json$/]);

  // Languages / runtimes / build systems by manifest presence.
  pushIf(out, ctx, "python", [/^pyproject\.toml$/, /^requirements\.txt$/, /^setup\.py$/, /^Pipfile$/]);
  pushIf(out, ctx, "rust", [/^Cargo\.toml$/]);
  pushIf(out, ctx, "go", [/^go\.mod$/]);
  pushIf(out, ctx, "ruby", [/^Gemfile$/, /\.gemspec$/]);
  pushIf(out, ctx, "elixir", [/^mix\.exs$/]);
  pushIf(out, ctx, "java", [/^pom\.xml$/, /^build\.gradle(\.kts)?$/]);
  pushIf(out, ctx, "cmake", [/^CMakeLists\.txt$/]);
  pushIf(out, ctx, "zig", [/^build\.zig$/]);
  pushIf(out, ctx, "deno", [/^deno\.jsonc?$/]);
  pushIf(out, ctx, "bun", [/^bun\.lockb?$/, /^bunfig\.toml$/]);
  pushIf(out, ctx, "laravel", [/^artisan$/]);
  pushIf(out, ctx, "php", [/^composer\.json$/]);
  pushIf(out, ctx, "dotnet", [/\.(csproj|sln)$/]);

  // Game engines.
  pushIf(out, ctx, "unity", [/ProjectSettings\/ProjectVersion\.txt$/, /\.unity$/]);
  pushIf(out, ctx, "godot", [/^project\.godot$/]);
  pushIf(out, ctx, "unreal", [/\.uproject$/]);

  // Monorepo orchestrators.
  pushIf(out, ctx, "turborepo", [/^turbo\.json$/]);
  pushIf(out, ctx, "nx", [/^nx\.json$/]);

  return dedupe(out);
}

function pushIf(out: Framework[], ctx: DetectContext, id: string, files: RegExp[]): void {
  for (const re of files) {
    const f = ctx.scan.files.find((x) => re.test(x.rel.split("/").pop() ?? "") || re.test(x.rel));
    if (f) {
      out.push({ id, confidence: "detected", evidence: [{ kind: "file", file: f.rel }] });
      return;
    }
  }
}

function dedupe(list: Framework[]): Framework[] {
  const map = new Map<string, Framework>();
  for (const f of list) if (!map.has(f.id)) map.set(f.id, f);
  return [...map.values()];
}
