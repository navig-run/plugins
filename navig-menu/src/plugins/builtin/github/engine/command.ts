/**
 * Pure argv builders for the `gh` CLI operations the plugin exposes. Each returns the arguments that
 * follow the `gh` binary (the handler prepends `gh`). Kept pure so they're unit-testable and the exec
 * path stays an argv ARRAY (no shell) — a PR number or tag can never be reinterpreted as a shell
 * token. `--json` variants ask gh for structured output the handlers parse to drive pickers.
 */

/* ── repo ────────────────────────────────────────────────────────────────────────── */

export function repoViewArgs(web = false): string[] {
  return web ? ["repo", "view", "--web"] : ["repo", "view"];
}
export function repoViewJsonArgs(fields: string[]): string[] {
  return ["repo", "view", "--json", fields.join(",")];
}
export function browseArgs(): string[] {
  return ["browse"];
}
export function authStatusArgs(): string[] {
  return ["auth", "status"];
}

/* ── pull requests ───────────────────────────────────────────────────────────────── */

export interface ListOpts {
  state?: string;
  limit?: number;
}
export function prListArgs(o: ListOpts = {}): string[] {
  const a = ["pr", "list"];
  if (o.state) a.push("--state", o.state);
  if (o.limit) a.push("--limit", String(o.limit));
  return a;
}
export function prListJsonArgs(fields: string[], o: ListOpts = {}): string[] {
  const a = ["pr", "list", "--json", fields.join(",")];
  if (o.state) a.push("--state", o.state);
  if (o.limit) a.push("--limit", String(o.limit));
  return a;
}
export function prListWebArgs(): string[] {
  return ["pr", "list", "--web"];
}
export function prViewArgs(n: string, web = false): string[] {
  const a = ["pr", "view", n];
  if (web) a.push("--web");
  return a;
}
export function prCheckoutArgs(n: string): string[] {
  return ["pr", "checkout", n];
}
export function prCreateArgs(): string[] {
  return ["pr", "create"];
}
export function prStatusArgs(): string[] {
  return ["pr", "status"];
}

/* ── issues ──────────────────────────────────────────────────────────────────────── */

export function issueListArgs(o: ListOpts = {}): string[] {
  const a = ["issue", "list"];
  if (o.state) a.push("--state", o.state);
  if (o.limit) a.push("--limit", String(o.limit));
  return a;
}
export function issueListJsonArgs(fields: string[], o: ListOpts = {}): string[] {
  const a = ["issue", "list", "--json", fields.join(",")];
  if (o.state) a.push("--state", o.state);
  if (o.limit) a.push("--limit", String(o.limit));
  return a;
}
export function issueListWebArgs(): string[] {
  return ["issue", "list", "--web"];
}
export function issueViewArgs(n: string, web = false): string[] {
  const a = ["issue", "view", n];
  if (web) a.push("--web");
  return a;
}
export function issueCreateArgs(): string[] {
  return ["issue", "create"];
}

/* ── actions / CI ────────────────────────────────────────────────────────────────── */

export function runListArgs(limit = 20): string[] {
  return ["run", "list", "--limit", String(limit)];
}
export function runListJsonArgs(fields: string[], limit = 1): string[] {
  return ["run", "list", "--json", fields.join(","), "--limit", String(limit)];
}
export function runViewArgs(id: string, o: { web?: boolean; log?: boolean } = {}): string[] {
  const a = ["run", "view", id];
  if (o.web) a.push("--web");
  if (o.log) a.push("--log");
  return a;
}
export function runWatchArgs(id: string): string[] {
  return ["run", "watch", id];
}
export function runRerunArgs(id: string, failedOnly = false): string[] {
  const a = ["run", "rerun", id];
  if (failedOnly) a.push("--failed");
  return a;
}

/* ── releases ────────────────────────────────────────────────────────────────────── */

export function releaseListArgs(): string[] {
  return ["release", "list"];
}
export function releaseCreateArgs(tag: string, o: { generateNotes?: boolean } = {}): string[] {
  const a = ["release", "create", tag];
  if (o.generateNotes) a.push("--generate-notes");
  return a;
}
