/**
 * Pure argv builders for every wrangler operation the plugin exposes. Each returns the arguments
 * that follow the `wrangler` binary (the launcher + any `wrangler` prefix are prepended by the
 * handler). Keeping these pure makes them trivially unit-testable and keeps the exec path an argv
 * ARRAY (no shell string), so a database name or SQL string can never be interpreted as a shell
 * token. Flags mirror wrangler v3/v4 (`kv namespace`, `d1 execute --local|--remote`, etc.).
 */

/* ── Workers ─────────────────────────────────────────────────────────────────────── */

export interface DeployOpts {
  env?: string;
  dryRun?: boolean;
  outdir?: string;
}
export function deployArgs(o: DeployOpts = {}): string[] {
  const a = ["deploy"];
  if (o.dryRun) a.push("--dry-run");
  if (o.outdir) a.push("--outdir", o.outdir);
  if (o.env) a.push("--env", o.env);
  return a;
}

export function devArgs(o: { env?: string } = {}): string[] {
  const a = ["dev"];
  if (o.env) a.push("--env", o.env);
  return a;
}

export function tailArgs(o: { env?: string } = {}): string[] {
  const a = ["tail"];
  if (o.env) a.push("--env", o.env);
  return a;
}

export function typesArgs(): string[] {
  return ["types"];
}
export function whoamiArgs(): string[] {
  return ["whoami"];
}
export function loginArgs(): string[] {
  return ["login"];
}
export function deploymentsListArgs(o: { env?: string } = {}): string[] {
  const a = ["deployments", "list"];
  if (o.env) a.push("--env", o.env);
  return a;
}
export function versionsListArgs(): string[] {
  return ["versions", "list"];
}
export function rollbackArgs(o: { id?: string; env?: string } = {}): string[] {
  const a = ["rollback"];
  if (o.id) a.push(o.id);
  if (o.env) a.push("--env", o.env);
  return a;
}

/* ── Pages ───────────────────────────────────────────────────────────────────────── */

export function pagesDevArgs(dir: string): string[] {
  return ["pages", "dev", dir];
}
export function pagesDeployArgs(dir: string, o: { project?: string; branch?: string; commitDirty?: boolean } = {}): string[] {
  const a = ["pages", "deploy", dir];
  if (o.project) a.push("--project-name", o.project);
  if (o.branch) a.push("--branch", o.branch);
  if (o.commitDirty) a.push("--commit-dirty=true");
  return a;
}
export function pagesProjectListArgs(): string[] {
  return ["pages", "project", "list"];
}
export function pagesDeploymentListArgs(project?: string): string[] {
  const a = ["pages", "deployment", "list"];
  if (project) a.push("--project-name", project);
  return a;
}
export function pagesDeploymentTailArgs(project?: string): string[] {
  const a = ["pages", "deployment", "tail"];
  if (project) a.push("--project-name", project);
  return a;
}

/* ── D1 ──────────────────────────────────────────────────────────────────────────── */

export interface D1ExecOpts {
  remote?: boolean;
  command?: string;
  file?: string;
}
export function d1ExecuteArgs(db: string, o: D1ExecOpts): string[] {
  const a = ["d1", "execute", db, o.remote ? "--remote" : "--local"];
  if (o.command) a.push("--command", o.command);
  if (o.file) a.push("--file", o.file);
  return a;
}
export function d1ListArgs(): string[] {
  return ["d1", "list"];
}
export function d1CreateArgs(name: string): string[] {
  return ["d1", "create", name];
}
export function d1MigrationsApplyArgs(db: string, o: { remote?: boolean } = {}): string[] {
  return ["d1", "migrations", "apply", db, o.remote ? "--remote" : "--local"];
}
export function d1MigrationsListArgs(db: string, o: { remote?: boolean } = {}): string[] {
  return ["d1", "migrations", "list", db, o.remote ? "--remote" : "--local"];
}
export function d1MigrationsCreateArgs(db: string, message: string): string[] {
  return ["d1", "migrations", "create", db, message];
}
export function d1ExportArgs(db: string, output: string, o: { remote?: boolean } = {}): string[] {
  // Explicit target — never rely on wrangler's version-dependent default (v3 = remote),
  // so a "local" export can't silently dump the production database.
  return ["d1", "export", db, "--output", output, o.remote ? "--remote" : "--local"];
}

/* ── KV ──────────────────────────────────────────────────────────────────────────── */

export interface KvKeyOpts {
  binding?: string;
  namespaceId?: string;
  remote?: boolean;
  preview?: boolean;
}
function kvTarget(o: KvKeyOpts): string[] {
  const a: string[] = [];
  if (o.binding) a.push("--binding", o.binding);
  else if (o.namespaceId) a.push("--namespace-id", o.namespaceId);
  if (o.preview) a.push("--preview");
  // Always explicit. Wrangler v3 defaulted key ops to REMOTE, so an implicit
  // (no-flag) "local" op would silently hit production — the handler labels that
  // path "local (safe)". Mirror `d1 execute`, which is explicit for the same reason.
  a.push(o.remote ? "--remote" : "--local");
  return a;
}
export function kvNamespaceListArgs(): string[] {
  return ["kv", "namespace", "list"];
}
export function kvNamespaceCreateArgs(title: string): string[] {
  return ["kv", "namespace", "create", title];
}
export function kvKeyListArgs(o: KvKeyOpts): string[] {
  return ["kv", "key", "list", ...kvTarget(o)];
}
export function kvKeyGetArgs(key: string, o: KvKeyOpts): string[] {
  return ["kv", "key", "get", key, ...kvTarget(o)];
}
export function kvKeyPutArgs(key: string, value: string, o: KvKeyOpts): string[] {
  return ["kv", "key", "put", key, value, ...kvTarget(o)];
}
export function kvKeyDeleteArgs(key: string, o: KvKeyOpts): string[] {
  return ["kv", "key", "delete", key, ...kvTarget(o)];
}

/* ── R2 ──────────────────────────────────────────────────────────────────────────── */

export function r2BucketListArgs(): string[] {
  return ["r2", "bucket", "list"];
}
export function r2BucketCreateArgs(name: string): string[] {
  return ["r2", "bucket", "create", name];
}
export function r2ObjectGetArgs(objectPath: string, o: { file?: string; remote?: boolean } = {}): string[] {
  const a = ["r2", "object", "get", objectPath];
  if (o.file) a.push("--file", o.file);
  if (o.remote) a.push("--remote");
  return a;
}
export function r2ObjectPutArgs(objectPath: string, o: { file?: string; remote?: boolean } = {}): string[] {
  const a = ["r2", "object", "put", objectPath];
  if (o.file) a.push("--file", o.file);
  if (o.remote) a.push("--remote");
  return a;
}
export function r2ObjectDeleteArgs(objectPath: string, o: { remote?: boolean } = {}): string[] {
  const a = ["r2", "object", "delete", objectPath];
  if (o.remote) a.push("--remote");
  return a;
}

/* ── Secrets (Workers + Pages variants) ────────────────────────────────────────────── */

export interface SecretOpts {
  env?: string;
  pages?: boolean;
  project?: string;
}
function secretHeadTail(o: SecretOpts): { head: string[]; tail: string[] } {
  if (o.pages) return { head: ["pages", "secret"], tail: o.project ? ["--project-name", o.project] : [] };
  return { head: ["secret"], tail: o.env ? ["--env", o.env] : [] };
}
export function secretListArgs(o: SecretOpts): string[] {
  const b = secretHeadTail(o);
  return [...b.head, "list", ...b.tail];
}
export function secretPutArgs(name: string, o: SecretOpts): string[] {
  const b = secretHeadTail(o);
  return [...b.head, "put", name, ...b.tail];
}
export function secretDeleteArgs(name: string, o: SecretOpts): string[] {
  const b = secretHeadTail(o);
  return [...b.head, "delete", name, ...b.tail];
}

/* ── Queues / Vectorize / Hyperdrive ────────────────────────────────────────────────── */

export function queuesListArgs(): string[] {
  return ["queues", "list"];
}
export function queuesCreateArgs(name: string): string[] {
  return ["queues", "create", name];
}
export function vectorizeListArgs(): string[] {
  return ["vectorize", "list"];
}
export function hyperdriveListArgs(): string[] {
  return ["hyperdrive", "list"];
}
