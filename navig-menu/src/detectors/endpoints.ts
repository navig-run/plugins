import { basename, join } from "node:path";
import type { DetectContext, PackageJson } from "./context.js";
import { readJson, readText } from "./fs.js";
import type { Endpoint, Evidence } from "../manifest/schema.js";

/**
 * Best-effort discovery of the local URLs a project serves, so the banner can show the
 * 🔭 webapp / ⚡ api rows the way schema's dev-banner does — but generically, for any repo.
 *
 * Pure reads only. A row is emitted ONLY when we can tie a real dev/serve script to a port
 * (script flag → env file → framework convention), so we never invent endpoints that don't
 * exist. Scheme is https when a script opts into TLS or a cert sits next to the package.
 */

interface Candidate {
  /** Directory relative to root ("" = root). */
  dir: string;
  pkg: PackageJson;
}

const DEV_SCRIPT_KEYS = ["dev", "start", "serve", "develop", "preview"];

/** Conventional dev ports by framework dependency — the fallback when nothing explicit is found. */
const FRAMEWORK_PORTS: Array<{ dep: string; port: number; kind: Endpoint["kind"] }> = [
  { dep: "next", port: 3000, kind: "web" },
  { dep: "nuxt", port: 3000, kind: "web" },
  { dep: "@remix-run/dev", port: 3000, kind: "web" },
  { dep: "@react-router/dev", port: 3000, kind: "web" },
  { dep: "astro", port: 4321, kind: "web" },
  { dep: "@sveltejs/kit", port: 5173, kind: "web" },
  { dep: "gatsby", port: 8000, kind: "web" },
  { dep: "vite", port: 5173, kind: "web" },
  { dep: "@nestjs/core", port: 3000, kind: "api" },
  { dep: "fastify", port: 3000, kind: "api" },
  { dep: "hono", port: 8787, kind: "api" },
  { dep: "express", port: 3000, kind: "api" },
];

const API_NAME = /\b(api|server|backend|service|worker|gateway|edge)\b/i;
/** Names that are libraries / configs, not served surfaces — never an endpoint. */
const NON_SERVER = /^(shared|ui|config|configs|eslint|eslint-rules|tsconfig|types|typings|utils|util|lib|libs|tokens|theme|themes|assets|fixtures|playwright|e2e|test|tests)$/i;

interface ScoredEndpoint {
  endpoint: Endpoint;
  /** Explicit ports (script flag / env file) are trustworthy; framework defaults are a guess. */
  explicit: boolean;
}

export function detectEndpoints(ctx: DetectContext, workspacePackages: string[]): Endpoint[] {
  const candidates: Candidate[] = [];
  if (ctx.pkg) candidates.push({ dir: "", pkg: ctx.pkg });
  for (const rel of workspacePackages) {
    const pkg = readJson<PackageJson>(join(ctx.root, rel, "package.json"));
    if (pkg) candidates.push({ dir: rel, pkg });
  }

  const scored: ScoredEndpoint[] = [];
  const seen = new Set<string>();
  for (const cand of candidates) {
    const ep = endpointFor(ctx, cand);
    if (!ep) continue;
    if (seen.has(ep.endpoint.url)) continue;
    seen.add(ep.endpoint.url);
    scored.push(ep);
  }

  // Confirmed (explicit) endpoints first so the primary app (e.g. webapp on :443/TLS) always
  // wins over framework-default guesses; then web before api, then by label. Cap to stay tight.
  scored.sort(
    (a, b) =>
      Number(b.explicit) - Number(a.explicit) ||
      kindRank(a.endpoint.kind) - kindRank(b.endpoint.kind) ||
      a.endpoint.label.localeCompare(b.endpoint.label),
  );
  return scored.slice(0, 4).map((s) => s.endpoint);
}

function endpointFor(ctx: DetectContext, cand: Candidate): ScoredEndpoint | undefined {
  const scripts = cand.pkg.scripts ?? {};
  const devKey = DEV_SCRIPT_KEYS.find((k) => scripts[k]);
  if (!devKey) return undefined;
  const script = scripts[devKey]!;

  const name = cand.pkg.name ? unscope(cand.pkg.name) : cand.dir ? basename(cand.dir) : "app";
  if (NON_SERVER.test(name)) return undefined;

  // A pure orchestrator (only delegates to other scripts/packages) has no port of its own.
  if (/--filter|turbo |concurrently|run-p|npm-run-all/.test(script) && !/(-p|--port|-H|--host)\b/.test(script)) {
    return undefined;
  }

  const deps = depsOf(cand.pkg);
  const fw = FRAMEWORK_PORTS.find((f) => deps[f.dep] !== undefined);
  const portFromScript = parsePort(script);
  const portFromEnv = portFromEnvFiles(ctx, cand.dir);
  const explicitPort = portFromScript ?? portFromEnv;
  const port = explicitPort ?? fw?.port;
  if (port === undefined) return undefined;

  const kind: Endpoint["kind"] = API_NAME.test(name) || fw?.kind === "api" ? "api" : "web";

  const tls = looksTls(ctx, cand.dir, script);
  const scheme = tls ? "https" : "http";
  const isDefaultPort = (scheme === "https" && port === 443) || (scheme === "http" && (port === 80 || port === 0));
  const url = `${scheme}://localhost${isDefaultPort ? "" : ":" + port}`;

  const evidence: Evidence[] = [
    { kind: "script", file: join(cand.dir || ".", "package.json"), detail: devKey },
  ];

  return {
    explicit: explicitPort !== undefined,
    endpoint: {
      id: cand.dir || name,
      label: kind === "api" && !API_NAME.test(name) ? `${name} api` : name,
      kind,
      url,
      tls,
      evidence,
    },
  };
}

function depsOf(pkg: PackageJson): Record<string, string> {
  return {
    ...(pkg.dependencies ?? {}),
    ...(pkg.devDependencies ?? {}),
    ...(pkg.optionalDependencies ?? {}),
    ...(pkg.peerDependencies ?? {}),
  };
}

function parsePort(script: string): number | undefined {
  const m =
    script.match(/(?:-p|--port)[=\s]+(\d{2,5})/) ??
    script.match(/\bPORT[=\s]+(\d{2,5})/);
  return m ? Number(m[1]) : undefined;
}

function portFromEnvFiles(ctx: DetectContext, dir: string): number | undefined {
  for (const file of [".env.local", ".env.development", ".env"]) {
    const raw = readText(join(ctx.root, dir, file));
    if (!raw) continue;
    const m = raw.match(/^\s*PORT\s*=\s*"?(\d{2,5})"?\s*$/m);
    if (m) return Number(m[1]);
  }
  return undefined;
}

function looksTls(ctx: DetectContext, dir: string, script: string): boolean {
  if (/--experimental-https|https:\/\/|--ssl|--tls|HTTPS\s*=\s*true|\bsslKey\b/i.test(script)) return true;
  // A cert sitting next to the package is a strong signal (schema's adhoc-server.key pattern).
  const certHints = [
    "certificates/adhoc-server.key",
    "certs/server.key",
    "certificates/localhost.key",
    "localhost-key.pem",
    "cert/key.pem",
  ];
  return certHints.some((rel) => ctx.absExists(join(dir, rel)));
}

function unscope(name: string): string {
  const slash = name.lastIndexOf("/");
  return slash >= 0 ? name.slice(slash + 1) : name;
}

function kindRank(kind: Endpoint["kind"]): number {
  return kind === "web" ? 0 : kind === "api" ? 1 : 2;
}
