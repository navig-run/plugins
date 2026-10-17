/**
 * Interactive handlers for the GitHub plugin — the only place with side effects (spawning `gh`,
 * prompting, cache writes). Everything runs the real `gh` CLI as an argv ARRAY (no shell) via the
 * sandboxed `run`/`capture`; pure argv builders live in `engine/command.ts`. `gh` manages its own
 * auth, so no credentials pass through the plugin. The one branch-changing action (PR checkout) and
 * the one publishing action (create a release) confirm first.
 */

import type { PluginActionContext } from "../../types.js";
import type { GithubStatsCache } from "./banner.js";
import * as cmd from "./engine/command.js";

/* ── plumbing ──────────────────────────────────────────────────────────────────────── */

function ghrun(a: PluginActionContext, args: string[]): Promise<number> {
  return a.run("gh", args);
}
function ghcap(a: PluginActionContext, args: string[]): Promise<{ stdout: string; exitCode: number }> {
  return a.capture("gh", args);
}
function safeJson<T>(text: string): T | undefined {
  try {
    return JSON.parse(text) as T;
  } catch {
    return undefined;
  }
}
/** gh must be installed for any action; check once and tell the user how to get it if not. */
async function ensureGh(a: PluginActionContext): Promise<boolean> {
  const r = await ghcap(a, ["--version"]);
  if (r.exitCode !== 0) {
    a.notify("GitHub CLI (gh) not found — install it from https://cli.github.com");
    return false;
  }
  return true;
}
/** Pick "open" / "all" / create / open-in-browser for a list view. */
async function listMode(a: PluginActionContext, noun: string): Promise<"open" | "all" | "create" | "web" | undefined> {
  const picked = await a.select({
    message: noun,
    choices: [
      { name: "open", message: `Browse open ${noun.toLowerCase()}…` },
      { name: "all", message: `Browse all ${noun.toLowerCase()}…` },
      { name: "create", message: `Create ${noun === "Issues" ? "an issue" : "a PR"}` },
      { name: "web", message: "Open in browser" },
    ],
  });
  return picked as "open" | "all" | "create" | "web" | undefined;
}

/* ── pull requests ───────────────────────────────────────────────────────────────── */

interface PrRow {
  number: number;
  title: string;
  author?: { login?: string };
}

export async function pullRequests(a: PluginActionContext): Promise<void> {
  if (!(await ensureGh(a))) return;
  const mode = await listMode(a, "Pull requests");
  if (mode === undefined) return a.notify("cancelled");
  if (mode === "create") return void (await ghrun(a, cmd.prCreateArgs()));
  if (mode === "web") return void (await ghrun(a, cmd.prListWebArgs()));

  const res = await ghcap(a, cmd.prListJsonArgs(["number", "title", "author"], { state: mode, limit: 30 }));
  const prs = safeJson<PrRow[]>(res.stdout);
  if (!prs?.length) return a.notify(`no ${mode === "all" ? "" : "open "}pull requests`);
  const picked = await a.select({
    message: "Which PR?",
    choices: prs.map((p) => ({ name: String(p.number), message: `#${p.number} ${p.title}`, hint: p.author?.login })),
  });
  if (picked === undefined) return a.notify("cancelled");

  const act = await a.select({
    message: `PR #${picked}`,
    choices: [
      { name: "view", message: "View details" },
      { name: "web", message: "Open in browser" },
      { name: "checkout", message: "Check out locally (switches your branch)" },
    ],
  });
  switch (act) {
    case "view":
      await ghrun(a, cmd.prViewArgs(picked));
      return;
    case "web":
      await ghrun(a, cmd.prViewArgs(picked, true));
      return;
    case "checkout":
      if (!(await a.confirm(`Check out PR #${picked}? This switches your working branch.`))) return a.notify("cancelled");
      await ghrun(a, cmd.prCheckoutArgs(picked));
      return;
    default:
      return a.notify("cancelled");
  }
}

/* ── issues ──────────────────────────────────────────────────────────────────────── */

interface IssueRow {
  number: number;
  title: string;
  author?: { login?: string };
}

export async function issues(a: PluginActionContext): Promise<void> {
  if (!(await ensureGh(a))) return;
  const mode = await listMode(a, "Issues");
  if (mode === undefined) return a.notify("cancelled");
  if (mode === "create") return void (await ghrun(a, cmd.issueCreateArgs()));
  if (mode === "web") return void (await ghrun(a, cmd.issueListWebArgs()));

  const res = await ghcap(a, cmd.issueListJsonArgs(["number", "title", "author"], { state: mode, limit: 30 }));
  const list = safeJson<IssueRow[]>(res.stdout);
  if (!list?.length) return a.notify(`no ${mode === "all" ? "" : "open "}issues`);
  const picked = await a.select({
    message: "Which issue?",
    choices: list.map((i) => ({ name: String(i.number), message: `#${i.number} ${i.title}`, hint: i.author?.login })),
  });
  if (picked === undefined) return a.notify("cancelled");
  const act = await a.select({
    message: `Issue #${picked}`,
    choices: [
      { name: "view", message: "View details" },
      { name: "web", message: "Open in browser" },
    ],
  });
  if (act === "view") return void (await ghrun(a, cmd.issueViewArgs(picked)));
  if (act === "web") return void (await ghrun(a, cmd.issueViewArgs(picked, true)));
  return a.notify("cancelled");
}

/* ── actions / CI ────────────────────────────────────────────────────────────────── */

interface RunRow {
  databaseId: number;
  displayTitle?: string;
  headBranch?: string;
  status?: string;
  conclusion?: string;
}

export async function actions(a: PluginActionContext): Promise<void> {
  if (!(await ensureGh(a))) return;
  const res = await ghcap(a, cmd.runListJsonArgs(["databaseId", "displayTitle", "headBranch", "status", "conclusion"], 20));
  const runs = safeJson<RunRow[]>(res.stdout);
  if (!runs?.length) return a.notify("no workflow runs found");
  const picked = await a.select({
    message: "Which run?",
    choices: runs.map((r) => ({
      name: String(r.databaseId),
      message: `${r.conclusion || r.status || "?"} · ${r.displayTitle ?? "(run)"}`,
      hint: r.headBranch,
    })),
  });
  if (picked === undefined) return a.notify("cancelled");
  const act = await a.select({
    message: `Run ${picked}`,
    choices: [
      { name: "view", message: "View summary" },
      { name: "log", message: "View full log" },
      { name: "web", message: "Open in browser" },
      { name: "watch", message: "Watch live" },
      { name: "rerun", message: "Re-run failed jobs" },
    ],
  });
  switch (act) {
    case "view":
      await ghrun(a, cmd.runViewArgs(picked));
      return;
    case "log":
      await ghrun(a, cmd.runViewArgs(picked, { log: true }));
      return;
    case "web":
      await ghrun(a, cmd.runViewArgs(picked, { web: true }));
      return;
    case "watch":
      await ghrun(a, cmd.runWatchArgs(picked));
      return;
    case "rerun":
      if (!(await a.confirm(`Re-run the failed jobs of run ${picked}?`))) return a.notify("cancelled");
      await ghrun(a, cmd.runRerunArgs(picked, true));
      return;
    default:
      return a.notify("cancelled");
  }
}

/* ── releases ────────────────────────────────────────────────────────────────────── */

export async function releases(a: PluginActionContext): Promise<void> {
  if (!(await ensureGh(a))) return;
  const op = await a.select({
    message: "Releases",
    choices: [
      { name: "list", message: "List releases" },
      { name: "create", message: "Create a release (publishes to GitHub)" },
    ],
  });
  if (op === "list") return void (await ghrun(a, cmd.releaseListArgs()));
  if (op === "create") {
    const tag = await a.input("Tag for the new release (e.g. v1.2.0):");
    if (!tag) return a.notify("cancelled");
    if (!(await a.confirm(`Create & publish release "${tag}" on GitHub?`))) return a.notify("cancelled");
    await ghrun(a, cmd.releaseCreateArgs(tag, { generateNotes: true }));
    return;
  }
  return a.notify("cancelled");
}

/* ── overview / auth / refresh ─────────────────────────────────────────────────────── */

export async function status(a: PluginActionContext): Promise<void> {
  if (!(await ensureGh(a))) return;
  await ghrun(a, cmd.prStatusArgs());
}

export async function repoView(a: PluginActionContext): Promise<void> {
  if (!(await ensureGh(a))) return;
  await ghrun(a, cmd.repoViewArgs());
}

export async function openWeb(a: PluginActionContext): Promise<void> {
  if (!(await ensureGh(a))) return;
  await ghrun(a, cmd.browseArgs());
}

export async function authStatus(a: PluginActionContext): Promise<void> {
  if (!(await ensureGh(a))) return;
  await ghrun(a, cmd.authStatusArgs());
}

interface RepoJson {
  stargazerCount?: number;
  defaultBranchRef?: { name?: string };
}

export async function refreshStats(a: PluginActionContext): Promise<void> {
  if (!(await ensureGh(a))) return;
  const repo = safeJson<RepoJson>((await ghcap(a, cmd.repoViewJsonArgs(["stargazerCount", "defaultBranchRef"]))).stdout);
  const prs = safeJson<unknown[]>((await ghcap(a, cmd.prListJsonArgs(["number"], { state: "open", limit: 100 }))).stdout);
  const runs = safeJson<Array<{ conclusion?: string; status?: string }>>(
    (await ghcap(a, cmd.runListJsonArgs(["conclusion", "status"], 1))).stdout,
  );
  const cache: GithubStatsCache = {
    stars: repo?.stargazerCount,
    openPRs: Array.isArray(prs) ? prs.length : undefined,
    ci: runs?.[0]?.conclusion || runs?.[0]?.status,
    defaultBranch: repo?.defaultBranchRef?.name,
    fetchedAt: Date.now(),
  };
  a.writeCache("stats", cache);
  a.notify(
    `GitHub stats refreshed${cache.stars != null ? ` · ★${cache.stars}` : ""}${cache.openPRs != null ? ` · ${cache.openPRs} open PRs` : ""}`,
  );
}
