import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { basename, join } from "node:path";
import { definePlugin } from "../types.js";
import type { PluginActionContext, PluginContext } from "../types.js";

/**
 * Dev Host — surfaces `navig devhost` inside `navig menu`: set up a trusted-HTTPS
 * `.test` domain for the current project, start/stop the relay, check status, open
 * it. A built-in navig-menu plugin: it auto-activates for web projects and drives
 * the `navig devhost` CLI (the real engine — hosts + mkcert + a TLS relay) via the
 * sanctioned `run`/`capture` primitives. If the `navig` CLI isn't installed, the
 * actions report that cleanly instead of failing.
 */

/* ── shapes ───────────────────────────────────────────────────────────────────── */

interface RegistryEntry {
  target_port?: number;
  [k: string]: unknown;
}
interface Registry {
  domains?: Record<string, RegistryEntry>;
}
export interface MatchedDomain {
  domain: string;
  target_port?: number;
  configured: boolean;
}
/** A row from `navig devhost list --json`. */
interface DevhostDomain {
  domain: string;
  url: string;
  target: string;
  hosts_ok?: boolean;
  cert_ok?: boolean;
  target_up?: boolean;
  serving?: boolean;
}

type PortsCtx = { manifest?: { endpoints?: Array<{ url?: string }> } };
type RootCtx = { root?: string };

/* ── pure helpers (safe in detect/contribute; unit-tested) ─────────────────────── */

/** Read the devhost registry the Python engine writes, best-effort (→ null). */
export function readRegistry(): Registry | null {
  const candidates = [
    process.env.NAVIG_CONFIG_DIR ? join(process.env.NAVIG_CONFIG_DIR, "devhost", "registry.json") : undefined,
    join(homedir(), ".navig", "devhost", "registry.json"),
  ].filter((p): p is string => Boolean(p));
  for (const p of candidates) {
    try {
      return JSON.parse(readFileSync(p, "utf8")) as Registry;
    } catch {
      /* not there / unreadable — best effort */
    }
  }
  return null;
}

/** The ports THIS project serves on, parsed from its detected endpoints. */
export function projectPorts(ctx: PortsCtx): number[] {
  const set = new Set<number>();
  for (const ep of ctx.manifest?.endpoints ?? []) {
    const m = String(ep?.url || "").match(/:(\d+)(?:\/|$)/);
    if (m) set.add(Number(m[1]));
  }
  return [...set];
}

/** A `<slug>.test` domain guessed from the project folder name. */
export function guessDomain(ctx: RootCtx): string {
  const slug = basename(ctx.root || "app")
    .replace(/[^a-z0-9-]+/gi, "-")
    .replace(/^-+|-+$/g, "")
    .toLowerCase();
  return `${slug || "app"}.test`;
}

/** The registered devhost domain matching this project (by target port), else a guess. */
export function matchDomain(ctx: PortsCtx & RootCtx, reg: Registry | null = readRegistry()): MatchedDomain {
  const ports = projectPorts(ctx);
  if (reg?.domains) {
    for (const [name, d] of Object.entries(reg.domains)) {
      if (ports.includes(Number(d?.target_port))) return { domain: name, target_port: d?.target_port, configured: true };
    }
  }
  return { domain: guessDomain(ctx), target_port: ports[0], configured: false };
}

/* ── the plugin ────────────────────────────────────────────────────────────────── */

export const devhost = definePlugin({
  id: "devhost",
  tier: "programmatic",
  version: "1.0.0",
  // Active for any JS/web project (where a local HTTPS dev domain makes sense).
  detect: (ctx) => Boolean(ctx.hasFile?.("package.json")) || projectPorts(ctx).length > 0,
  contribute: (ctx) => {
    const info = matchDomain(ctx);
    return {
      bannerLines: info.configured ? [{ text: `🔒 https://${info.domain}`, tone: "green" as const }] : [],
      sections: [
        {
          group: "Dev Host",
          meta: { title: "DEV HOST", emoji: "🌐", unicode: "◉", ascii: "@", tone: "cyan" },
          actions: [
            {
              id: "devhost.setup",
              label: info.configured ? "Reconfigure HTTPS domain" : "Set up HTTPS domain",
              internal: "setup",
              description: "trusted https://<name>.test in front of this dev server",
              risk: "confirm",
            },
            {
              id: "devhost.up",
              label: "Start HTTPS (relay)",
              internal: "up",
              description: "serve https://<domain> — Ctrl+C to stop",
              longRunning: true,
            },
            { id: "devhost.open", label: "Open in browser", internal: "open" },
            { id: "devhost.status", label: "Status", internal: "status", description: "hosts · cert · dev-up · serving" },
            { id: "devhost.remove", label: "Remove domain", internal: "remove", risk: "confirm" },
          ],
        },
      ],
      about: ["devhost — local .test domains with trusted HTTPS (drives `navig devhost`)"],
    };
  },
  handlers: {
    setup: setupHandler,
    up: upHandler,
    open: openHandler,
    status: statusHandler,
    remove: removeHandler,
  },
});

/* ── handlers (side effects allowed) ───────────────────────────────────────────── */

/** Guard: the built-in ships with navig-menu, but the engine is the `navig` CLI. */
async function ensureNavig(a: PluginActionContext): Promise<boolean> {
  try {
    const { exitCode } = await a.capture("navig", ["--version"]);
    if (exitCode === 0) return true;
  } catch {
    /* not resolvable on PATH */
  }
  a.notify("navig CLI not found — install navig + the navig-devhost plugin");
  return false;
}

async function listDomains(a: PluginActionContext): Promise<DevhostDomain[]> {
  try {
    const { stdout, exitCode } = await a.capture("navig", ["devhost", "list", "--json"]);
    if (exitCode !== 0) return [];
    return (JSON.parse(stdout).domains ?? []) as DevhostDomain[];
  } catch {
    return [];
  }
}

function forThisProject(a: PluginActionContext, domains: DevhostDomain[]): DevhostDomain | undefined {
  const ports = projectPorts(a);
  return domains.find((d) => ports.includes(Number(String(d.target).match(/:(\d+)/)?.[1])));
}

async function pickDomain(a: PluginActionContext, domains: DevhostDomain[], message: string): Promise<string | undefined> {
  const mine = forThisProject(a, domains);
  if (mine) return mine.domain;
  if (domains.length === 1) return domains[0]!.domain;
  const picked = await a.select({
    message,
    choices: domains.map((d) => ({ name: d.domain, message: `${d.url}  →  ${d.target}` })),
  });
  return picked || undefined;
}

async function setupHandler(a: PluginActionContext): Promise<void> {
  if (!(await ensureNavig(a))) return;
  const info = matchDomain(a);
  const domain = (await a.input(`Domain [${info.domain}]:`)) || info.domain;
  const defPort = info.target_port ?? projectPorts(a)[0];
  const answer = (await a.input(`Dev server port${defPort ? ` [${defPort}]` : ""}:`)) || (defPort ? String(defPort) : "");
  const port = Number(String(answer).trim());
  if (!Number.isInteger(port) || port <= 0 || port > 65535) return a.notify("cancelled — a valid port is required");
  const ok = await a.confirm(`Add ${domain} → :${port}?  (edits the hosts file — needs an elevated terminal)`);
  if (!ok) return a.notify("cancelled");
  const code = await a.run("navig", ["devhost", "add", domain, "--port", String(port)]);
  a.notify(code === 0 ? `✓ ${domain} ready — pick "Start HTTPS (relay)"` : `add failed (exit ${code}) — run this terminal as Administrator`);
}

async function upHandler(a: PluginActionContext): Promise<void> {
  if (!(await ensureNavig(a))) return;
  const domains = await listDomains(a);
  if (!domains.length) return a.notify('no domains yet — run "Set up HTTPS domain" first');
  const domain = await pickDomain(a, domains, "Serve which domain?");
  if (!domain) return a.notify("cancelled");
  a.notify(`starting relay for ${domain} — Ctrl+C to stop`);
  await a.run("navig", ["devhost", "up", domain]); // foreground until Ctrl+C
}

async function openHandler(a: PluginActionContext): Promise<void> {
  if (!(await ensureNavig(a))) return;
  const domains = await listDomains(a);
  const target = forThisProject(a, domains) ?? domains[0];
  if (!target) return a.notify('no domain configured — run "Set up HTTPS domain" first');
  if (process.platform === "win32") await a.run("cmd", ["/c", "start", "", target.url]);
  else if (process.platform === "darwin") await a.run("open", [target.url]);
  else await a.run("xdg-open", [target.url]);
  a.notify(`opening ${target.url}`);
}

async function statusHandler(a: PluginActionContext): Promise<void> {
  if (!(await ensureNavig(a))) return;
  const domains = await listDomains(a);
  if (!domains.length) return a.notify("no devhost domains configured");
  const c = a.theme?.c;
  const flag = (b: boolean | undefined) => (b ? (c ? c.green("✓") : "✓") : c ? c.red("✗") : "✗");
  console.log("");
  for (const d of domains) {
    console.log(
      `  ${d.url}  →  ${d.target}   ` +
        `hosts ${flag(d.hosts_ok)}  cert ${flag(d.cert_ok)}  dev ${flag(d.target_up)}  serving ${flag(d.serving)}`,
    );
  }
  a.notify(`${domains.length} devhost domain(s)`);
}

async function removeHandler(a: PluginActionContext): Promise<void> {
  if (!(await ensureNavig(a))) return;
  const domains = await listDomains(a);
  if (!domains.length) return a.notify("nothing to remove");
  const domain =
    domains.length === 1
      ? domains[0]!.domain
      : await a.select({ message: "Remove which domain?", choices: domains.map((d) => ({ name: d.domain, message: d.url })) });
  if (!domain) return a.notify("cancelled");
  if (!(await a.confirm(`Remove ${domain} (hosts entry + cert)?`))) return a.notify("cancelled");
  const code = await a.run("navig", ["devhost", "remove", domain]);
  a.notify(code === 0 ? `✓ removed ${domain}` : `remove failed (exit ${code}) — needs an elevated terminal`);
}
