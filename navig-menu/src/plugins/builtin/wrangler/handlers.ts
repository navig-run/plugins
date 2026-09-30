/**
 * Interactive handlers for the Cloudflare plugin — the only place with side effects (spawning
 * wrangler, prompting, cache writes). Everything runs the real `wrangler` binary as an argv ARRAY
 * (no shell) via the sandboxed `run`/`capture`; pure argv builders live in `engine/command.ts`.
 *
 * SAFETY: destructive/production actions (`--remote` D1, deploy, delete, rollback) confirm
 * explicitly here — internal plugin actions bypass the menu's own risk gate, so the guard lives in
 * the handler. Secret VALUES are never handled: `wrangler secret put` reads the value on its own
 * stdin (inherited), and only the secret NAME ever passes through us.
 */

import type { PluginActionContext } from "../../types.js";
import { loadWranglerConfig, type WranglerConfig } from "./config.js";
import { classifyType } from "./engine/classify.js";
import { resolveWranglerInvocation } from "./engine/launcher.js";
import { parseWhoami } from "./engine/whoami.js";
import * as cmd from "./engine/command.js";

/* ── shared plumbing ───────────────────────────────────────────────────────────────── */

function inv(a: PluginActionContext) {
  const runner = typeof a.settings.runner === "string" ? a.settings.runner : "auto";
  return resolveWranglerInvocation(a.root, runner);
}
/** Run wrangler with inherited stdio (dev servers / tail / interactive prompts keep their TTY). */
function wrun(a: PluginActionContext, args: string[]): Promise<number> {
  const i = inv(a);
  return a.run(i.launcher, [...i.prefix, ...args]);
}
/** Run wrangler and capture stdout (for parsing, e.g. whoami). */
function wcap(a: PluginActionContext, args: string[]): Promise<{ stdout: string; exitCode: number }> {
  const i = inv(a);
  return a.capture(i.launcher, [...i.prefix, ...args]);
}
function cfg(a: PluginActionContext): WranglerConfig | undefined {
  return loadWranglerConfig(a.root);
}
function ptype(a: PluginActionContext, c = cfg(a)): "worker" | "pages" {
  return classifyType(c, a.scripts);
}

/** Second guard before any production/`--remote` op (in addition to per-action confirms). */
function guardRemote(a: PluginActionContext, what: string): Promise<boolean> {
  if (a.settings.remoteConfirm === false) return Promise.resolve(true);
  return a.confirm(`This runs against PRODUCTION — ${what}. Continue?`);
}

const DEFAULT_ENV = "__default__";
/** Pick an environment. Returns "" for the top-level/default env, an env name, or undefined (cancel). */
async function pickEnv(a: PluginActionContext, c: WranglerConfig | undefined, message: string): Promise<string | undefined> {
  const envs = c?.envs ?? [];
  if (!envs.length) return "";
  const choices = [{ name: DEFAULT_ENV, message: "default (top-level)" }, ...envs.map((e) => ({ name: e, message: `--env ${e}` }))];
  const picked = await a.select({ message, choices });
  if (picked === undefined) return undefined;
  return picked === DEFAULT_ENV ? "" : picked;
}

async function pickTarget(a: PluginActionContext, label: string): Promise<"local" | "remote" | undefined> {
  const t = await a.select({
    message: `${label} — target?`,
    choices: [
      { name: "local", message: "local (dev, safe)" },
      { name: "remote", message: "remote (PRODUCTION)" },
    ],
  });
  return t === undefined ? undefined : (t as "local" | "remote");
}

/** Pick from known binding/resource names, with a "type your own" escape. */
async function pickName(a: PluginActionContext, names: string[], label: string): Promise<string | undefined> {
  const list = names.filter(Boolean);
  if (list.length === 1) return list[0];
  const choices = list.map((n) => ({ name: n, message: n }));
  choices.push({ name: "__custom", message: `type a ${label}…` });
  let picked = await a.select({ message: `${label}?`, choices });
  if (picked === undefined) return undefined;
  if (picked === "__custom") picked = await a.input(`${label}:`);
  return picked || undefined;
}

/* ── Workers / Pages lifecycle ─────────────────────────────────────────────────────── */

export async function dev(a: PluginActionContext): Promise<void> {
  const c = cfg(a);
  if (ptype(a, c) === "pages") {
    const dir = c?.pagesBuildOutputDir ?? (await a.input("Static output dir to serve (e.g. dist):"));
    if (!dir) return a.notify("cancelled");
    await wrun(a, cmd.pagesDevArgs(dir));
    return;
  }
  const env = await pickEnv(a, c, "Dev — which environment?");
  if (env === undefined) return a.notify("cancelled");
  await wrun(a, cmd.devArgs({ env: env || undefined }));
}

export async function deploy(a: PluginActionContext): Promise<void> {
  const c = cfg(a);
  if (ptype(a, c) === "pages") {
    const dir = c?.pagesBuildOutputDir ?? (await a.input("Directory to deploy (e.g. dist):"));
    if (!dir) return a.notify("cancelled");
    const project = await a.input(`Pages project name${c?.name ? ` [${c.name}]` : ""}:`);
    if (project === undefined) return a.notify("cancelled");
    if (!(await guardRemote(a, "Pages deploy"))) return a.notify("cancelled");
    await wrun(a, cmd.pagesDeployArgs(dir, { project: project || c?.name }));
    return;
  }
  const env = await pickEnv(a, c, "Deploy — which environment?");
  if (env === undefined) return a.notify("cancelled");
  if (!(await guardRemote(a, `deploy${env ? ` --env ${env}` : ""}`))) return a.notify("cancelled");
  await wrun(a, cmd.deployArgs({ env: env || undefined }));
}

export async function tail(a: PluginActionContext): Promise<void> {
  const c = cfg(a);
  if (ptype(a, c) === "pages") {
    await wrun(a, cmd.pagesDeploymentTailArgs(c?.name));
    return;
  }
  const env = await pickEnv(a, c, "Tail logs — environment?");
  if (env === undefined) return a.notify("cancelled");
  await wrun(a, cmd.tailArgs({ env: env || undefined }));
}

export async function deployments(a: PluginActionContext): Promise<void> {
  const c = cfg(a);
  if (ptype(a, c) === "pages") {
    await wrun(a, cmd.pagesDeploymentListArgs(c?.name));
    return;
  }
  const op = await a.select({
    message: "Deployments",
    choices: [
      { name: "list", message: "List deployments" },
      { name: "versions", message: "List versions" },
      { name: "rollback", message: "Roll back to a previous version (production)" },
    ],
  });
  switch (op) {
    case "list":
      await wrun(a, cmd.deploymentsListArgs());
      return;
    case "versions":
      await wrun(a, cmd.versionsListArgs());
      return;
    case "rollback": {
      const id = await a.input("Version id to roll back to (blank = previous):");
      if (id === undefined) return a.notify("cancelled");
      if (!(await guardRemote(a, "roll back the live deployment"))) return a.notify("cancelled");
      await wrun(a, cmd.rollbackArgs({ id: id || undefined }));
      return;
    }
    default:
      return a.notify("cancelled");
  }
}

/* ── D1 ────────────────────────────────────────────────────────────────────────────── */

async function pickDatabase(a: PluginActionContext, c: WranglerConfig | undefined): Promise<string | undefined> {
  const names = (c?.d1 ?? []).map((d) => d.databaseName).filter(Boolean) as string[];
  const choices = names.map((n) => ({ name: n, message: n }));
  choices.push({ name: "__list", message: "list databases (live)…" });
  choices.push({ name: "__custom", message: "type a database name…" });
  let picked = await a.select({ message: "Which D1 database?", choices });
  if (picked === undefined) return undefined;
  if (picked === "__list") {
    await wrun(a, cmd.d1ListArgs());
    picked = await a.input("Database name:");
  } else if (picked === "__custom") {
    picked = await a.input("Database name:");
  }
  return picked || undefined;
}

export async function d1(a: PluginActionContext): Promise<void> {
  const c = cfg(a);
  const db = await pickDatabase(a, c);
  if (!db) return a.notify("cancelled");
  const op = await a.select({
    message: `D1 · ${db}`,
    choices: [
      { name: "query", message: "Run SQL (--command)" },
      { name: "file", message: "Execute a .sql file (--file)" },
      { name: "mlist", message: "Migrations: list" },
      { name: "mcreate", message: "Migrations: create" },
      { name: "mlocal", message: "Migrations: apply (local)" },
      { name: "mremote", message: "Migrations: apply (remote / production)" },
      { name: "export", message: "Export to a .sql file" },
    ],
  });
  switch (op) {
    case "query": {
      const sql = await a.input("SQL:");
      if (!sql) return a.notify("cancelled");
      const t = await pickTarget(a, "D1 query");
      if (!t) return a.notify("cancelled");
      if (t === "remote" && !(await guardRemote(a, `run SQL on ${db}`))) return a.notify("cancelled");
      await wrun(a, cmd.d1ExecuteArgs(db, { command: sql, remote: t === "remote" }));
      return;
    }
    case "file": {
      const file = await a.input(".sql file path:");
      if (!file) return a.notify("cancelled");
      const t = await pickTarget(a, "D1 file");
      if (!t) return a.notify("cancelled");
      if (t === "remote" && !(await guardRemote(a, `execute ${file} on ${db}`))) return a.notify("cancelled");
      await wrun(a, cmd.d1ExecuteArgs(db, { file, remote: t === "remote" }));
      return;
    }
    case "mlist": {
      const t = await pickTarget(a, "Migrations list");
      if (!t) return a.notify("cancelled");
      await wrun(a, cmd.d1MigrationsListArgs(db, { remote: t === "remote" }));
      return;
    }
    case "mcreate": {
      const msg = await a.input("Migration message:");
      if (!msg) return a.notify("cancelled");
      await wrun(a, cmd.d1MigrationsCreateArgs(db, msg));
      return;
    }
    case "mlocal":
      await wrun(a, cmd.d1MigrationsApplyArgs(db, { remote: false }));
      return;
    case "mremote": {
      if (!(await guardRemote(a, `apply migrations to ${db}`))) return a.notify("cancelled");
      await wrun(a, cmd.d1MigrationsApplyArgs(db, { remote: true }));
      return;
    }
    case "export": {
      const out = await a.input("Output .sql file:");
      if (!out) return a.notify("cancelled");
      const t = await pickTarget(a, "D1 export");
      if (!t) return a.notify("cancelled");
      await wrun(a, cmd.d1ExportArgs(db, out, { remote: t === "remote" }));
      return;
    }
    default:
      return a.notify("cancelled");
  }
}

/* ── KV ────────────────────────────────────────────────────────────────────────────── */

export async function kv(a: PluginActionContext): Promise<void> {
  const c = cfg(a);
  const op = await a.select({
    message: "KV",
    choices: [
      { name: "nslist", message: "Namespaces: list (live)" },
      { name: "nscreate", message: "Namespaces: create" },
      { name: "keys", message: "Browse / edit keys in a namespace…" },
    ],
  });
  if (op === undefined) return a.notify("cancelled");
  if (op === "nslist") return void (await wrun(a, cmd.kvNamespaceListArgs()));
  if (op === "nscreate") {
    const title = await a.input("New namespace title (binding name):");
    if (!title) return a.notify("cancelled");
    await wrun(a, cmd.kvNamespaceCreateArgs(title));
    return;
  }
  // keys
  const binding = await pickName(a, (c?.kv ?? []).map((k) => k.binding), "KV binding");
  if (!binding) return a.notify("cancelled");
  const t = await pickTarget(a, "KV");
  if (!t) return a.notify("cancelled");
  const remote = t === "remote";
  const kop = await a.select({
    message: `KV · ${binding} (${t})`,
    choices: [
      { name: "list", message: "List keys" },
      { name: "get", message: "Get a value" },
      { name: "put", message: "Put a value" },
      { name: "delete", message: "Delete a key" },
    ],
  });
  const opts = { binding, remote };
  switch (kop) {
    case "list":
      await wrun(a, cmd.kvKeyListArgs(opts));
      return;
    case "get": {
      const key = await a.input("Key:");
      if (!key) return a.notify("cancelled");
      await wrun(a, cmd.kvKeyGetArgs(key, opts));
      return;
    }
    case "put": {
      const key = await a.input("Key:");
      if (!key) return a.notify("cancelled");
      const value = await a.input("Value:");
      if (value === undefined) return a.notify("cancelled");
      if (remote && !(await guardRemote(a, `write "${key}" to ${binding}`))) return a.notify("cancelled");
      await wrun(a, cmd.kvKeyPutArgs(key, value, opts));
      return;
    }
    case "delete": {
      const key = await a.input("Key to delete:");
      if (!key) return a.notify("cancelled");
      if (!(await a.confirm(`Delete "${key}" from ${binding}${remote ? " (PRODUCTION)" : ""}?`))) return a.notify("cancelled");
      await wrun(a, cmd.kvKeyDeleteArgs(key, opts));
      return;
    }
    default:
      return a.notify("cancelled");
  }
}

/* ── R2 ────────────────────────────────────────────────────────────────────────────── */

async function r2ObjectPath(a: PluginActionContext, c: WranglerConfig | undefined): Promise<string | undefined> {
  const bucket = await pickName(a, (c?.r2 ?? []).map((r) => r.bucketName).filter(Boolean) as string[], "bucket name");
  if (!bucket) return undefined;
  const key = await a.input("Object key (path within the bucket):");
  if (!key) return undefined;
  return `${bucket}/${key}`;
}

export async function r2(a: PluginActionContext): Promise<void> {
  const c = cfg(a);
  const op = await a.select({
    message: "R2",
    choices: [
      { name: "blist", message: "Buckets: list" },
      { name: "bcreate", message: "Buckets: create" },
      { name: "put", message: "Object: upload a file" },
      { name: "get", message: "Object: download" },
      { name: "delete", message: "Object: delete" },
    ],
  });
  switch (op) {
    case "blist":
      await wrun(a, cmd.r2BucketListArgs());
      return;
    case "bcreate": {
      const name = await a.input("New bucket name:");
      if (!name) return a.notify("cancelled");
      await wrun(a, cmd.r2BucketCreateArgs(name));
      return;
    }
    case "put": {
      const path = await r2ObjectPath(a, c);
      if (!path) return a.notify("cancelled");
      const file = await a.input("Local file to upload (--file):");
      if (!file) return a.notify("cancelled");
      if (!(await guardRemote(a, `upload to ${path}`))) return a.notify("cancelled");
      await wrun(a, cmd.r2ObjectPutArgs(path, { file, remote: true }));
      return;
    }
    case "get": {
      const path = await r2ObjectPath(a, c);
      if (!path) return a.notify("cancelled");
      const file = await a.input("Save to file (--file):");
      if (!file) return a.notify("cancelled");
      await wrun(a, cmd.r2ObjectGetArgs(path, { file, remote: true }));
      return;
    }
    case "delete": {
      const path = await r2ObjectPath(a, c);
      if (!path) return a.notify("cancelled");
      if (!(await a.confirm(`Delete R2 object "${path}" (PRODUCTION)?`))) return a.notify("cancelled");
      await wrun(a, cmd.r2ObjectDeleteArgs(path, { remote: true }));
      return;
    }
    default:
      return a.notify("cancelled");
  }
}

/* ── Secrets (never touch values) ──────────────────────────────────────────────────── */

export async function secrets(a: PluginActionContext): Promise<void> {
  const c = cfg(a);
  const pages = ptype(a, c) === "pages";
  let env = "";
  let project = pages ? c?.name : undefined;
  if (pages && !project) {
    // `wrangler pages secret …` requires --project-name; prompt when the config has no name.
    const p = await a.input("Pages project name:");
    if (!p) return a.notify("cancelled");
    project = p;
  }
  if (!pages && (c?.envs?.length ?? 0) > 0) {
    const e = await pickEnv(a, c, "Secrets — environment?");
    if (e === undefined) return a.notify("cancelled");
    env = e;
  }
  const opts = { pages, project, env: env || undefined };
  const op = await a.select({
    message: "Secrets",
    choices: [
      { name: "list", message: "List secret names" },
      { name: "put", message: "Set a secret (value entered securely in wrangler)" },
      { name: "delete", message: "Delete a secret" },
    ],
  });
  switch (op) {
    case "list":
      await wrun(a, cmd.secretListArgs(opts));
      return;
    case "put": {
      const name = await a.input("Secret NAME (its value is typed into wrangler, never stored here):");
      if (!name) return a.notify("cancelled");
      // A secret put always mutates the LIVE Worker/Pages project (there is no local
      // secret store) and overwrites any current value — confirm before it does.
      const where = pages ? `Pages project ${project}` : "the live Worker";
      const scope = env ? ` (--env ${env})` : "";
      if (!(await a.confirm(`Set secret "${name}" on ${where}${scope}? This overwrites any current value.`)))
        return a.notify("cancelled");
      await wrun(a, cmd.secretPutArgs(name, opts));
      return;
    }
    case "delete": {
      const name = await a.input("Secret NAME to delete:");
      if (!name) return a.notify("cancelled");
      if (!(await a.confirm(`Delete secret "${name}"?`))) return a.notify("cancelled");
      await wrun(a, cmd.secretDeleteArgs(name, opts));
      return;
    }
    default:
      return a.notify("cancelled");
  }
}

/* ── Queues / Vectorize / Hyperdrive ────────────────────────────────────────────────── */

export async function queues(a: PluginActionContext): Promise<void> {
  const op = await a.select({
    message: "Queues",
    choices: [
      { name: "list", message: "List queues" },
      { name: "create", message: "Create a queue" },
    ],
  });
  if (op === "list") return void (await wrun(a, cmd.queuesListArgs()));
  if (op === "create") {
    const name = await a.input("Queue name:");
    if (!name) return a.notify("cancelled");
    await wrun(a, cmd.queuesCreateArgs(name));
    return;
  }
  return a.notify("cancelled");
}

export async function vectorize(a: PluginActionContext): Promise<void> {
  await wrun(a, cmd.vectorizeListArgs());
}

export async function hyperdrive(a: PluginActionContext): Promise<void> {
  await wrun(a, cmd.hyperdriveListArgs());
}

/* ── typegen / auth ─────────────────────────────────────────────────────────────────── */

export async function types(a: PluginActionContext): Promise<void> {
  await wrun(a, cmd.typesArgs());
}

async function cacheWhoami(a: PluginActionContext): Promise<boolean> {
  const res = await wcap(a, cmd.whoamiArgs());
  const info = parseWhoami(res.stdout);
  if (info.email || info.account) {
    a.writeCache("whoami", { ...info, fetchedAt: Date.now() });
    return true;
  }
  return false;
}

export async function whoami(a: PluginActionContext): Promise<void> {
  await wrun(a, cmd.whoamiArgs());
  await cacheWhoami(a);
}

export async function login(a: PluginActionContext): Promise<void> {
  await wrun(a, cmd.loginArgs());
  await cacheWhoami(a);
}
