import { describe, it, expect } from "vitest";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { stripJsonc } from "../src/plugins/builtin/wrangler/engine/jsonc.js";
import {
  parseWranglerRaw,
  normalizeWranglerConfig,
  loadWranglerConfig,
  findWranglerConfig,
} from "../src/plugins/builtin/wrangler/config.js";
import { classifyType, summarize } from "../src/plugins/builtin/wrangler/engine/classify.js";
import { wranglerBannerLines } from "../src/plugins/builtin/wrangler/banner.js";
import { parseWhoami } from "../src/plugins/builtin/wrangler/engine/whoami.js";
import { findLocalWrangler, resolveWranglerInvocation } from "../src/plugins/builtin/wrangler/engine/launcher.js";
import * as cmd from "../src/plugins/builtin/wrangler/engine/command.js";
import { wrangler } from "../src/plugins/builtin/wrangler/index.js";
import { secrets } from "../src/plugins/builtin/wrangler/handlers.js";
import type { PluginActionContext, PluginContext } from "../src/plugins/types.js";

/* ── jsonc ─────────────────────────────────────────────────────────────────────────── */

describe("stripJsonc", () => {
  it("strips line + block comments and trailing commas", () => {
    const src = `{
      // a comment
      "name": "api", /* inline */
      "list": [1, 2, 3,],
      "obj": { "a": 1, },
    }`;
    expect(JSON.parse(stripJsonc(src))).toEqual({ name: "api", list: [1, 2, 3], obj: { a: 1 } });
  });

  it("never touches // or commas inside strings", () => {
    const src = `{ "url": "https://x.dev/a", "note": "a, b, c" }`;
    expect(JSON.parse(stripJsonc(src))).toEqual({ url: "https://x.dev/a", note: "a, b, c" });
  });

  it("handles escaped quotes inside strings", () => {
    const src = `{ "q": "she said \\"hi\\"", "x": 1, }`;
    expect(JSON.parse(stripJsonc(src))).toEqual({ q: 'she said "hi"', x: 1 });
  });
});

/* ── config parse + normalize ──────────────────────────────────────────────────────── */

const WORKER_TOML = `
name = "navig-api"
main = "src/index.ts"
compatibility_date = "2024-09-23"
compatibility_flags = ["nodejs_compat"]
workers_dev = true

[[d1_databases]]
binding = "DB"
database_name = "navig-db"
database_id = "abc-123"
migrations_dir = "migrations"

[[kv_namespaces]]
binding = "FLAGS"
id = "kv123"

[[r2_buckets]]
binding = "MEDIA"
bucket_name = "navig-media"

[ai]
binding = "AI"

[[vectorize]]
binding = "VEC"
index_name = "navig-zones"

[[hyperdrive]]
binding = "HD"
id = "hd-1"

[[services]]
binding = "EDGE"
service = "schema-edge"

[[durable_objects.bindings]]
name = "ROOM"
class_name = "CallRoom"

[[send_email]]
name = "MAIL"

[triggers]
crons = ["0 * * * *"]

[env.production]
name = "navig-api-prod"

[env.staging]
`;

describe("normalizeWranglerConfig (TOML worker)", () => {
  const cfg = normalizeWranglerConfig(parseWranglerRaw(WORKER_TOML, "toml"), "wrangler.toml", "toml");

  it("reads identity + type", () => {
    expect(cfg.name).toBe("navig-api");
    expect(cfg.main).toBe("src/index.ts");
    expect(cfg.type).toBe("worker");
    expect(cfg.compatibilityDate).toBe("2024-09-23");
    expect(cfg.compatibilityFlags).toEqual(["nodejs_compat"]);
    expect(cfg.workersDev).toBe(true);
  });

  it("extracts every binding group", () => {
    expect(cfg.d1).toEqual([{ binding: "DB", databaseName: "navig-db", databaseId: "abc-123", migrationsDir: "migrations" }]);
    expect(cfg.kv).toEqual([{ binding: "FLAGS", id: "kv123" }]);
    expect(cfg.r2).toEqual([{ binding: "MEDIA", bucketName: "navig-media" }]);
    expect(cfg.ai).toBe(true);
    expect(cfg.vectorize).toEqual([{ binding: "VEC", target: "navig-zones" }]);
    expect(cfg.hyperdrive).toEqual([{ binding: "HD", target: "hd-1" }]);
    expect(cfg.services).toEqual([{ binding: "EDGE", target: "schema-edge" }]);
    expect(cfg.durableObjects).toEqual([{ name: "ROOM", className: "CallRoom", scriptName: undefined }]);
    expect(cfg.sendEmail).toEqual(["MAIL"]);
    expect(cfg.crons).toEqual(["0 * * * *"]);
  });

  it("lists environments and the migrations dir", () => {
    expect(cfg.envs.sort()).toEqual(["production", "staging"]);
    expect(cfg.migrationsDir).toBe("migrations");
  });

  it("summarizes binding counts", () => {
    expect(summarize(cfg)).toMatchObject({ d1: 1, kv: 1, r2: 1, do: 1, ai: true, vectorize: 1, hyperdrive: 1, services: 1 });
  });
});

const WORKER_JSONC = `{
  // navig api
  "name": "navig-api",
  "main": "src/index.ts",
  "compatibility_date": "2024-09-23",
  "d1_databases": [
    { "binding": "DB", "database_name": "navig-db", "database_id": "abc", },
  ],
  "durable_objects": { "bindings": [ { "name": "DO", "class_name": "Thing" } ] },
  "assets": { "directory": "./public" },
  "env": { "production": {} },
}`;

describe("normalizeWranglerConfig (JSONC worker)", () => {
  const cfg = normalizeWranglerConfig(parseWranglerRaw(WORKER_JSONC, "jsonc"), "wrangler.jsonc", "jsonc");
  it("parses jsonc with comments + trailing commas", () => {
    expect(cfg.name).toBe("navig-api");
    expect(cfg.d1[0]).toMatchObject({ binding: "DB", databaseName: "navig-db" });
    expect(cfg.durableObjects[0]).toMatchObject({ name: "DO", className: "Thing" });
    expect(cfg.assets).toBe(true);
    expect(cfg.envs).toEqual(["production"]);
  });
});

const PAGES_TOML = `
name = "navig-www"
pages_build_output_dir = "out"

[[d1_databases]]
binding = "DB"
database_name = "navig-db"

[env.production]
`;

describe("classifyType", () => {
  it("worker with a main entrypoint", () => {
    const cfg = normalizeWranglerConfig(parseWranglerRaw(WORKER_TOML, "toml"), "wrangler.toml", "toml");
    expect(classifyType(cfg, {})).toBe("worker");
  });

  it("pages via pages_build_output_dir", () => {
    const cfg = normalizeWranglerConfig(parseWranglerRaw(PAGES_TOML, "toml"), "wrangler.toml", "toml");
    expect(cfg.type).toBe("pages");
    expect(classifyType(cfg, {})).toBe("pages");
  });

  it("pages via a wrangler-pages script when no main is present", () => {
    const cfg = normalizeWranglerConfig(parseWranglerRaw(`name = "site"`, "toml"), "wrangler.toml", "toml");
    expect(classifyType(cfg, { deploy: "wrangler pages deploy dist --project-name site" })).toBe("pages");
  });

  it("a main entrypoint wins over a stray pages script (assets worker)", () => {
    const cfg = normalizeWranglerConfig(parseWranglerRaw(`name = "app"\nmain = "worker.js"`, "toml"), "wrangler.toml", "toml");
    expect(classifyType(cfg, { "pages:preview": "wrangler pages dev" })).toBe("worker");
  });
});

/* ── banner ────────────────────────────────────────────────────────────────────────── */

describe("wranglerBannerLines", () => {
  it("renders a config-derived stat line", () => {
    const dir = mkdtempSync(join(tmpdir(), "wr-banner-"));
    try {
      writeFileSync(join(dir, "wrangler.toml"), WORKER_TOML);
      const lines = wranglerBannerLines(makeCtx(dir, {}));
      expect(lines.length).toBeGreaterThan(0);
      const text = lines[0]!.text;
      expect(text).toContain("Cloudflare · navig-api · worker");
      expect(text).toContain("D1×1");
      expect(text).toContain("AI");
      expect(text).toContain("env: production/staging");
      expect(lines[0]!.tone).toBe("yellow");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("returns [] for a non-wrangler project", () => {
    const dir = mkdtempSync(join(tmpdir(), "wr-nobanner-"));
    try {
      expect(wranglerBannerLines(makeCtx(dir, {}))).toEqual([]);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

/* ── command builders ──────────────────────────────────────────────────────────────── */

describe("command builders", () => {
  it("deploy with env", () => {
    expect(cmd.deployArgs()).toEqual(["deploy"]);
    expect(cmd.deployArgs({ env: "production" })).toEqual(["deploy", "--env", "production"]);
    expect(cmd.deployArgs({ dryRun: true, outdir: "dist" })).toEqual(["deploy", "--dry-run", "--outdir", "dist"]);
  });

  it("d1 execute local vs remote", () => {
    expect(cmd.d1ExecuteArgs("navig-db", { command: "SELECT 1" })).toEqual(["d1", "execute", "navig-db", "--local", "--command", "SELECT 1"]);
    expect(cmd.d1ExecuteArgs("navig-db", { remote: true, file: "seed.sql" })).toEqual(["d1", "execute", "navig-db", "--remote", "--file", "seed.sql"]);
  });

  it("d1 migrations apply target", () => {
    expect(cmd.d1MigrationsApplyArgs("db", { remote: true })).toEqual(["d1", "migrations", "apply", "db", "--remote"]);
    expect(cmd.d1MigrationsApplyArgs("db")).toEqual(["d1", "migrations", "apply", "db", "--local"]);
  });

  it("pages deploy with project + branch", () => {
    expect(cmd.pagesDeployArgs("dist", { project: "navig-www", branch: "main" })).toEqual([
      "pages", "deploy", "dist", "--project-name", "navig-www", "--branch", "main",
    ]);
  });

  it("secrets: worker vs pages variants", () => {
    expect(cmd.secretPutArgs("API_KEY", { env: "production" })).toEqual(["secret", "put", "API_KEY", "--env", "production"]);
    expect(cmd.secretPutArgs("API_KEY", { pages: true, project: "site" })).toEqual(["pages", "secret", "put", "API_KEY", "--project-name", "site"]);
  });

  it("kv key list targets a binding + remote", () => {
    expect(cmd.kvKeyListArgs({ binding: "FLAGS", remote: true })).toEqual(["kv", "key", "list", "--binding", "FLAGS", "--remote"]);
  });

  it("kv key ops make the local/remote target EXPLICIT (no implicit prod default)", () => {
    // local (and the unspecified default) must emit --local, never nothing —
    // wrangler v3 defaulted key ops to remote/production.
    expect(cmd.kvKeyListArgs({ binding: "FLAGS", remote: false })).toEqual(["kv", "key", "list", "--binding", "FLAGS", "--local"]);
    expect(cmd.kvKeyGetArgs("k", { binding: "FLAGS" })).toEqual(["kv", "key", "get", "k", "--binding", "FLAGS", "--local"]);
    expect(cmd.kvKeyPutArgs("k", "v", { binding: "FLAGS", remote: true })).toEqual(["kv", "key", "put", "k", "v", "--binding", "FLAGS", "--remote"]);
    expect(cmd.kvKeyDeleteArgs("k", { binding: "FLAGS", remote: false })).toEqual(["kv", "key", "delete", "k", "--binding", "FLAGS", "--local"]);
  });

  it("d1 export makes the target explicit (a 'local' export can't dump production)", () => {
    expect(cmd.d1ExportArgs("db", "dump.sql")).toEqual(["d1", "export", "db", "--output", "dump.sql", "--local"]);
    expect(cmd.d1ExportArgs("db", "dump.sql", { remote: true })).toEqual(["d1", "export", "db", "--output", "dump.sql", "--remote"]);
  });

  it("r2 object delete", () => {
    expect(cmd.r2ObjectDeleteArgs("bucket/key.png", { remote: true })).toEqual(["r2", "object", "delete", "bucket/key.png", "--remote"]);
  });
});

/* ── whoami parse ──────────────────────────────────────────────────────────────────── */

describe("parseWhoami", () => {
  it("extracts email + account from a table", () => {
    const out = [
      "👋 You are logged in with an OAuth Token, associated with the email dev@navig.run.",
      "┌───────────────┬──────────────────────────────────┐",
      "│ Account Name  │ Account ID                       │",
      "├───────────────┼──────────────────────────────────┤",
      "│ Navig Ltd     │ 0123456789abcdef0123456789abcdef │",
      "└───────────────┴──────────────────────────────────┘",
    ].join("\n");
    expect(parseWhoami(out)).toEqual({ email: "dev@navig.run", account: "Navig Ltd" });
  });

  it("returns {} when not logged in", () => {
    expect(parseWhoami("You are not authenticated. Please run `wrangler login`.")).toEqual({});
  });
});

/* ── launcher ──────────────────────────────────────────────────────────────────────── */

describe("resolveWranglerInvocation", () => {
  it("falls back to npx when there is no local bin", () => {
    const dir = mkdtempSync(join(tmpdir(), "wr-"));
    try {
      expect(resolveWranglerInvocation(dir, "auto")).toEqual({ launcher: "npx", prefix: ["--yes", "wrangler"], source: "npx" });
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("honors explicit runners", () => {
    expect(resolveWranglerInvocation("/x", "pnpm")).toMatchObject({ launcher: "pnpm", prefix: ["dlx", "wrangler"] });
    expect(resolveWranglerInvocation("/x", "bun")).toMatchObject({ launcher: "bunx", prefix: ["wrangler"] });
  });

  it("prefers a project-local wrangler bin on auto", () => {
    const dir = mkdtempSync(join(tmpdir(), "wr-"));
    try {
      const bin = process.platform === "win32" ? "wrangler.cmd" : "wrangler";
      mkdirSync(join(dir, "node_modules", ".bin"), { recursive: true });
      writeFileSync(join(dir, "node_modules", ".bin", bin), "");
      expect(findLocalWrangler(dir)).toBe(join(dir, "node_modules", ".bin", bin));
      expect(resolveWranglerInvocation(dir, "auto").source).toBe("local");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

/* ── loadWranglerConfig + detect (end-to-end over a temp project) ───────────────────── */

describe("loadWranglerConfig + plugin.detect", () => {
  it("finds, reads and normalizes a wrangler.toml, and detect() fires", () => {
    const dir = mkdtempSync(join(tmpdir(), "wr-proj-"));
    try {
      writeFileSync(join(dir, "wrangler.toml"), WORKER_TOML);
      expect(findWranglerConfig(dir)).toEqual({ file: "wrangler.toml", format: "toml" });
      const cfg = loadWranglerConfig(dir);
      expect(cfg?.name).toBe("navig-api");
      expect(cfg?.d1[0]?.databaseName).toBe("navig-db");

      // A repo with its own wrangler deploy script still detects...
      const ctx = makeCtx(dir, { deploy: "wrangler deploy", "d1:seed": "wrangler d1 execute DB --file seed.sql" });
      expect(wrangler.detect?.(ctx)).toBe(true);
      const contribution = wrangler.contribute(ctx);
      const ids = (contribution.sections?.[0]?.actions ?? []).map((a) => a.id);
      expect(ids).toEqual(expect.arrayContaining(["cf.dev", "cf.deploy", "cf.tail", "cf.d1", "cf.kv", "cf.r2", "cf.secrets", "cf.vectorize", "cf.hyperdrive", "cf.types"]));
      // ...but does NOT claim those scripts into the rail (the cf.* actions already cover them — no dup).
      expect(contribution.claims ?? []).toEqual([]);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("returns undefined + detect() false for a non-wrangler project", () => {
    const dir = mkdtempSync(join(tmpdir(), "wr-none-"));
    try {
      expect(loadWranglerConfig(dir)).toBeUndefined();
      expect(wrangler.detect?.(makeCtx(dir, {}))).toBe(false);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

/* ── handlers: safety gates ──────────────────────────────────────────────────────── */

/** A recording PluginActionContext that feeds queued prompt answers and captures runs. */
function makeActionCtx(root: string, q: { selects?: string[]; inputs?: string[]; confirms?: boolean[] }) {
  const selects = [...(q.selects ?? [])];
  const inputs = [...(q.inputs ?? [])];
  const confirms = [...(q.confirms ?? [])];
  const runs: string[][] = [];
  const ctx = {
    root,
    scripts: {},
    settings: {},
    cacheDir: join(root, ".cache"),
    manifest: {} as never,
    theme: { c: {} } as never,
    hasFile: () => false,
    hasDep: () => false,
    readCache: () => undefined,
    writeCache: () => {},
    notify: () => {},
    run: async (_launcher: string, argv: string[]) => {
      runs.push(argv);
      return 0;
    },
    capture: async () => ({ stdout: "", exitCode: 0 }),
    select: async () => selects.shift(),
    input: async () => inputs.shift(),
    confirm: async () => confirms.shift() ?? false,
  } as unknown as PluginActionContext;
  return { ctx, runs };
}

describe("wrangler handlers · secret put confirm gate", () => {
  it("does not run wrangler when the overwrite is declined; runs it when confirmed", async () => {
    const dir = mkdtempSync(join(tmpdir(), "wr-secret-"));
    try {
      writeFileSync(join(dir, "wrangler.toml"), 'name = "t"\nmain = "src/index.ts"\n'); // a worker, no envs

      const declined = makeActionCtx(dir, { selects: ["put"], inputs: ["API_KEY"], confirms: [false] });
      await secrets(declined.ctx);
      expect(declined.runs).toEqual([]); // declined → the live secret is never touched

      const accepted = makeActionCtx(dir, { selects: ["put"], inputs: ["API_KEY"], confirms: [true] });
      await secrets(accepted.ctx);
      expect(accepted.runs).toHaveLength(1);
      expect(accepted.runs[0]).toEqual(expect.arrayContaining(["secret", "put", "API_KEY"]));
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

/** Minimal PluginContext for detect/contribute (which only read root/scripts/settings/fs). */
function makeCtx(root: string, scripts: Record<string, string>): PluginContext {
  return {
    root,
    manifest: { scripts } as never,
    scripts,
    settings: {},
    cacheDir: join(root, ".cache"),
    hasFile: () => false,
    hasDep: () => false,
    readCache: () => undefined,
  } as unknown as PluginContext;
}
