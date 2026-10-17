/**
 * Wrangler config contract for the plugin. Wrangler ships a project's Cloudflare definition in one
 * of three formats — `wrangler.toml` (most common), `wrangler.jsonc`, or `wrangler.json` — all using
 * the same snake_case keys. We parse whichever exists into a normalized {@link WranglerConfig} so the
 * plugin can build a config-aware palette (worker vs pages, per-binding actions) without any network
 * call. Parsing is pure and read-only; it never executes or prints secret values.
 */

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { parse as parseToml } from "smol-toml";
import { stripJsonc } from "./engine/jsonc.js";

/** Search order — first match wins (wrangler itself resolves in this order). */
export const WRANGLER_FILES = ["wrangler.toml", "wrangler.jsonc", "wrangler.json"] as const;

export type WranglerFormat = "toml" | "jsonc" | "json";
export type WranglerType = "worker" | "pages";

export interface D1Binding {
  binding: string;
  databaseName?: string;
  databaseId?: string;
  migrationsDir?: string;
}
export interface KVBinding {
  binding: string;
  id?: string;
}
export interface R2Binding {
  binding: string;
  bucketName?: string;
}
export interface DOBinding {
  name: string;
  className?: string;
  scriptName?: string;
}
/** vectorize (target = index_name), hyperdrive (target = id), service (target = service). */
export interface NamedBinding {
  binding: string;
  target?: string;
}
export interface QueueRef {
  binding?: string;
  queue: string;
}

export interface WranglerConfig {
  /** Config filename, relative to root (e.g. `wrangler.toml`). */
  file: string;
  format: WranglerFormat;
  name?: string;
  main?: string;
  /** Config-derived type — refined with scripts by `engine/classify.classifyType`. */
  type: WranglerType;
  compatibilityDate?: string;
  compatibilityFlags: string[];
  workersDev?: boolean;
  pagesBuildOutputDir?: string;
  /** Has a `[assets]` (Workers Static Assets) or legacy `[site]` block. */
  assets: boolean;
  /** Route patterns / custom domains, for display. */
  routes: string[];
  /** Names of `[env.*]` environments. */
  envs: string[];
  /** Top-level `migrations_dir`, else the first D1 binding's, else undefined (default "migrations"). */
  migrationsDir?: string;
  d1: D1Binding[];
  kv: KVBinding[];
  r2: R2Binding[];
  queues: { producers: QueueRef[]; consumers: QueueRef[] };
  durableObjects: DOBinding[];
  ai: boolean;
  vectorize: NamedBinding[];
  hyperdrive: NamedBinding[];
  services: NamedBinding[];
  /** `[[send_email]]` binding names. */
  sendEmail: string[];
  /** `[triggers] crons`. */
  crons: string[];
  /** The raw parsed object (advanced/escape hatch). */
  raw: Record<string, unknown>;
}

export function findWranglerConfig(root: string): { file: string; format: WranglerFormat } | undefined {
  for (const f of WRANGLER_FILES) {
    if (existsSync(join(root, f))) {
      const format: WranglerFormat = f.endsWith(".toml") ? "toml" : f.endsWith(".jsonc") ? "jsonc" : "json";
      return { file: f, format };
    }
  }
  return undefined;
}

/** Parse raw config text by format. Throws on malformed input (callers treat that as "no config"). */
export function parseWranglerRaw(text: string, format: WranglerFormat): Record<string, unknown> {
  if (format === "toml") return parseToml(text) as Record<string, unknown>;
  return JSON.parse(format === "jsonc" ? stripJsonc(text) : text) as Record<string, unknown>;
}

export function loadWranglerConfig(root: string): WranglerConfig | undefined {
  const found = findWranglerConfig(root);
  if (!found) return undefined;
  let raw: Record<string, unknown>;
  try {
    raw = parseWranglerRaw(readFileSync(join(root, found.file), "utf8"), found.format);
  } catch {
    return undefined; // malformed config → behave as if unconfigured (plugin still offers generics)
  }
  if (!raw || typeof raw !== "object") return undefined;
  return normalizeWranglerConfig(raw, found.file, found.format);
}

/* ── normalization (pure) ────────────────────────────────────────────────────────── */

const asArray = (v: unknown): Record<string, unknown>[] =>
  Array.isArray(v) ? v.filter((x): x is Record<string, unknown> => !!x && typeof x === "object") : [];
const asObject = (v: unknown): Record<string, unknown> =>
  v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
const asStr = (v: unknown): string | undefined => (typeof v === "string" ? v : undefined);
const asStrArray = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);

/** Normalize a raw wrangler config object (format-agnostic — keys are snake_case in both TOML and JSON). */
export function normalizeWranglerConfig(
  raw: Record<string, unknown>,
  file: string,
  format: WranglerFormat,
): WranglerConfig {
  const d1: D1Binding[] = asArray(raw.d1_databases).map((d) => ({
    binding: asStr(d.binding) ?? "",
    databaseName: asStr(d.database_name),
    databaseId: asStr(d.database_id),
    migrationsDir: asStr(d.migrations_dir),
  }));
  const kv: KVBinding[] = asArray(raw.kv_namespaces).map((k) => ({ binding: asStr(k.binding) ?? "", id: asStr(k.id) }));
  const r2: R2Binding[] = asArray(raw.r2_buckets).map((r) => ({
    binding: asStr(r.binding) ?? "",
    bucketName: asStr(r.bucket_name),
  }));
  const durableObjects: DOBinding[] = asArray(asObject(raw.durable_objects).bindings).map((d) => ({
    name: asStr(d.name) ?? "",
    className: asStr(d.class_name),
    scriptName: asStr(d.script_name),
  }));
  const queuesRaw = asObject(raw.queues);
  const queues = {
    producers: asArray(queuesRaw.producers).map((p) => ({ binding: asStr(p.binding), queue: asStr(p.queue) ?? "" })),
    consumers: asArray(queuesRaw.consumers).map((c) => ({ queue: asStr(c.queue) ?? "" })),
  };
  const vectorize: NamedBinding[] = asArray(raw.vectorize).map((v) => ({
    binding: asStr(v.binding) ?? "",
    target: asStr(v.index_name),
  }));
  const hyperdrive: NamedBinding[] = asArray(raw.hyperdrive).map((h) => ({
    binding: asStr(h.binding) ?? "",
    target: asStr(h.id),
  }));
  const services: NamedBinding[] = asArray(raw.services).map((s) => ({
    binding: asStr(s.binding) ?? "",
    target: asStr(s.service),
  }));
  const sendEmail = asArray(raw.send_email)
    .map((e) => asStr(e.name) ?? asStr(e.binding) ?? "")
    .filter(Boolean);

  const routes: string[] = [];
  for (const r of Array.isArray(raw.routes) ? raw.routes : []) {
    if (typeof r === "string") routes.push(r);
    else if (r && typeof r === "object") {
      const o = r as Record<string, unknown>;
      const v = asStr(o.pattern) ?? asStr(o.custom_domain) ?? asStr(o.zone_name);
      if (v) routes.push(v);
    }
  }
  if (asStr(raw.route)) routes.push(asStr(raw.route)!);

  const pagesBuildOutputDir = asStr(raw.pages_build_output_dir);
  const migrationsDir = asStr(raw.migrations_dir) ?? d1.find((d) => d.migrationsDir)?.migrationsDir;

  return {
    file,
    format,
    name: asStr(raw.name),
    main: asStr(raw.main),
    type: pagesBuildOutputDir ? "pages" : "worker",
    compatibilityDate: asStr(raw.compatibility_date),
    compatibilityFlags: asStrArray(raw.compatibility_flags),
    workersDev: typeof raw.workers_dev === "boolean" ? raw.workers_dev : undefined,
    pagesBuildOutputDir,
    assets: typeof raw.assets === "object" && raw.assets !== null ? true : typeof raw.site === "object" && raw.site !== null,
    routes,
    envs: Object.keys(asObject(raw.env)),
    migrationsDir,
    d1,
    kv,
    r2,
    queues,
    durableObjects,
    ai: typeof raw.ai === "object" && raw.ai !== null,
    vectorize,
    hyperdrive,
    services,
    sendEmail,
    crons: asStrArray(asObject(raw.triggers).crons),
    raw,
  };
}
