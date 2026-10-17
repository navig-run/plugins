import net from "node:net";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join, relative } from "node:path";
import { definePlugin } from "../types.js";
import type { PluginActionContext } from "../types.js";

/**
 * Ports pack — kill, inspect, and find local ports. Cross-platform (netstat + tasklist on Windows,
 * lsof elsewhere). If a kill fails, it surfaces the holding process so you know what to do next.
 * Auto-activates for projects that run dev servers. Options: random range, confirm-before-kill,
 * force-kill.
 */

export interface PortRow {
  port: number;
  pid: number;
  name?: string;
}

const RANGE_PRESETS = ["3000-3999", "8000-8999", "1024-65535", "49152-65535"];

export const ports = definePlugin({
  id: "ports",
  tier: "programmatic",
  version: "1.0.0",
  // Active for projects that actually serve on a port (detected dev/API endpoints).
  detect: (ctx) => ctx.manifest.endpoints.length > 0,
  contribute: () => ({
    settings: [
      { key: "range", label: "Ports · random range", type: "cycle", values: RANGE_PRESETS, default: "3000-3999" },
      { key: "confirm", label: "Ports · confirm before kill", type: "toggle", default: true },
      { key: "force", label: "Ports · force kill", type: "toggle", default: true },
    ],
    sections: [
      {
        group: "Ports",
        meta: { title: "PORTS", emoji: "🔌", unicode: "◒", ascii: "%", tone: "red" },
        actions: [
          { id: "ports.auto", label: "Free my dev ports (auto)", internal: "auto", description: "kill whatever is on this project's ports", risk: "confirm" },
          { id: "ports.swap", label: "Auto free port → .env", internal: "swap", description: "find a free port + write PORT to .env" },
          { id: "ports.kill", label: "Kill a port", internal: "kill", description: "pick or type a port → kill the process", risk: "confirm" },
          { id: "ports.list", label: "Active ports", internal: "list", description: "every listening port + its process" },
          { id: "ports.which", label: "What's on a port?", internal: "which", description: "find the process holding a port" },
          { id: "ports.free", label: "Find a free port", internal: "free", description: "a random open port in your range" },
        ],
      },
    ],
    about: ["ports plugin — kill / inspect / find / auto-free local ports"],
  }),
  handlers: {
    auto: autoHandler,
    swap: swapHandler,
    kill: killHandler,
    list: listHandler,
    which: whichHandler,
    free: freeHandler,
  },
});

/* ── handlers ──────────────────────────────────────────────────────────────────── */

/** Auto-free: kill whatever is holding THIS project's detected dev-server ports. */
async function autoHandler(a: PluginActionContext): Promise<void> {
  const c = a.theme.c;
  const wanted = endpointPorts(a.manifest);
  if (!wanted.length) return a.notify("no dev-server ports detected for this project");
  const active = await activePorts(a);
  const targets = active.filter((p) => wanted.includes(p.port));
  if (!targets.length) return a.notify(`dev ports already free: ${wanted.map((p) => ":" + p).join(" ")}`);

  console.log("\n  " + c.yellow("about to free this project's ports:"));
  for (const t of targets) console.log("  " + c.white(`:${t.port}`) + c.dim(`  pid ${t.pid}  ·  ${t.name ?? "?"}`));
  if (a.settings.confirm !== false) {
    const ok = await a.confirm(`Kill ${targets.length} process(es) on ${targets.map((t) => ":" + t.port).join(" ")}?`);
    if (!ok) return a.notify("cancelled");
  }
  let killed = 0;
  for (const t of targets) if ((await killPid(a, t.pid, a.settings.force !== false)) === 0) killed++;
  a.notify(`freed ${killed}/${targets.length} dev port${targets.length === 1 ? "" : "s"}`);
}

/** Auto-change: find a free port and (with consent) write PORT=<port> into .env / .env.local. */
async function swapHandler(a: PluginActionContext): Promise<void> {
  const c = a.theme.c;
  const [min, max] = parseRange(typeof a.settings.range === "string" ? a.settings.range : "3000-3999", [3000, 3999]);
  const port = await findFreePort(min, max);
  if (port === undefined) return a.notify(`no free port found in ${min}-${max}`);
  console.log("\n  " + c.greenBright.bold(`  ${port}  `) + c.dim(`free in ${min}-${max}`) + "\n");
  const ok = await a.confirm(`Write PORT=${port} to your env file?`);
  if (!ok) return a.notify(`free port: ${port} (not written)`);
  const file = pickEnvFile(a.root);
  let content = "";
  try {
    content = readFileSync(file, "utf8");
  } catch {
    /* new file */
  }
  writeFileSync(file, upsertEnvPort(content, port), "utf8");
  a.notify(`PORT=${port} → ${relative(a.root, file).split("\\").join("/")}`);
}

async function killHandler(a: PluginActionContext): Promise<void> {
  const c = a.theme.c;
  const list = await activePorts(a);
  let picked: string | undefined;
  if (list.length) {
    const choices = list.map((p) => ({ name: String(p.port), message: `:${p.port}  ·  pid ${p.pid}  ·  ${p.name ?? "?"}` }));
    choices.push({ name: "__custom", message: "type a port number…" });
    picked = await a.select({ message: "Kill which port?", choices });
    if (picked === undefined) return a.notify("cancelled");
    if (picked === "__custom") picked = await a.input("Port to kill:");
  } else {
    picked = await a.input("No listening ports detected — port to kill:");
  }
  const num = toPort(picked);
  if (num === undefined) return a.notify(picked ? `invalid port: ${picked}` : "cancelled");

  const target = list.find((p) => p.port === num) ?? (await activePorts(a)).find((p) => p.port === num);
  if (!target) return a.notify(`nothing is listening on :${num}`);

  if (a.settings.confirm !== false) {
    const ok = await a.confirm(`Kill pid ${target.pid} (${target.name ?? "?"}) on :${num}?`);
    if (!ok) return a.notify("cancelled");
  }

  const code = await killPid(a, target.pid, a.settings.force !== false);
  if (code === 0) {
    a.notify(`✓ freed :${num} — killed pid ${target.pid} (${target.name ?? "?"})`);
  } else {
    // Couldn't kill → the finder tells you exactly what still holds it.
    console.log("\n  " + c.yellow(`could not kill pid ${target.pid} (${target.name ?? "?"}) on :${num}`));
    console.log("  " + c.dim("still held by: ") + c.white(`${target.name ?? "?"} · pid ${target.pid}`));
    console.log(
      "  " +
        c.dim(
          process.platform === "win32"
            ? `try an elevated terminal, or:  taskkill /PID ${target.pid} /F /T`
            : `try:  sudo kill -9 ${target.pid}`,
        ),
    );
    a.notify(`could not free :${num} — see the holding process above`);
  }
}

async function listHandler(a: PluginActionContext): Promise<void> {
  const c = a.theme.c;
  const list = await activePorts(a);
  if (!list.length) return a.notify("no listening ports found (netstat/lsof unavailable?)");
  console.log("");
  for (const p of list) {
    console.log("  " + c.cyanBright(`:${String(p.port).padEnd(6)}`) + c.dim(`pid ${String(p.pid).padEnd(8)}`) + c.white(p.name ?? "?"));
  }
  a.notify(`${list.length} listening port${list.length === 1 ? "" : "s"}`);
}

async function whichHandler(a: PluginActionContext): Promise<void> {
  const picked = await a.input("Which port?");
  const num = toPort(picked);
  if (num === undefined) return a.notify(picked ? `invalid port: ${picked}` : "cancelled");
  const t = (await activePorts(a)).find((p) => p.port === num);
  a.notify(t ? `:${num} → pid ${t.pid} · ${t.name ?? "?"}` : `:${num} is free (nothing listening)`);
}

async function freeHandler(a: PluginActionContext): Promise<void> {
  const c = a.theme.c;
  const def = typeof a.settings.range === "string" ? a.settings.range : "3000-3999";
  const answer = await a.input(`Range for a random free port [${def}]:`);
  if (answer === undefined) return a.notify("cancelled");
  const [min, max] = parseRange(answer || def, [3000, 3999]);
  const port = await findFreePort(min, max);
  if (port === undefined) return a.notify(`no free port found in ${min}-${max}`);
  console.log("\n  " + c.greenBright.bold(`  ${port}  `) + c.dim(`free in ${min}-${max}`) + "\n");
  a.notify(`free port: ${port}`);
}

/* ── platform helpers ──────────────────────────────────────────────────────────── */

async function activePorts(a: PluginActionContext): Promise<PortRow[]> {
  if (process.platform === "win32") {
    const { stdout } = await a.capture("netstat", ["-ano", "-p", "TCP"]);
    const rows = parseNetstat(stdout);
    if (!rows.length) return [];
    const { stdout: tl } = await a.capture("tasklist", ["/FO", "CSV", "/NH"]);
    const names = parseTasklist(tl);
    return rows.map((r) => ({ ...r, name: names[r.pid] ?? "?" }));
  }
  const { stdout } = await a.capture("lsof", ["-nP", "-iTCP", "-sTCP:LISTEN"]);
  return parseLsof(stdout);
}

function killPid(a: PluginActionContext, pid: number, force: boolean): Promise<number> {
  return process.platform === "win32"
    ? a.run("taskkill", ["/PID", String(pid), ...(force ? ["/F"] : []), "/T"])
    : a.run("kill", [...(force ? ["-9"] : []), String(pid)]);
}

/* ── pure parsers (unit-tested) ────────────────────────────────────────────────── */

/** Parse `netstat -ano -p TCP` LISTENING lines → { port, pid }, deduped by port. */
export function parseNetstat(text: string): PortRow[] {
  const byPort = new Map<number, PortRow>();
  for (const line of text.split(/\r?\n/)) {
    const t = line.trim();
    if (!/^TCP\b/i.test(t) || !/\bLISTENING\b/i.test(t)) continue;
    const cols = t.split(/\s+/);
    const local = cols[1] ?? "";
    const pid = Number(cols[cols.length - 1]);
    const m = local.match(/:(\d+)$/);
    if (!m || !Number.isInteger(pid)) continue;
    const port = Number(m[1]);
    if (!byPort.has(port)) byPort.set(port, { port, pid });
  }
  return [...byPort.values()].sort((x, y) => x.port - y.port);
}

/** Parse `lsof -nP -iTCP -sTCP:LISTEN` → { port, pid, name }, deduped by port. */
export function parseLsof(text: string): PortRow[] {
  const byPort = new Map<number, PortRow>();
  for (const line of text.split(/\r?\n/)) {
    if (!/\(LISTEN\)/.test(line)) continue;
    const cols = line.trim().split(/\s+/);
    const name = cols[0] ?? "?";
    const pid = Number(cols[1]);
    const addr = cols[cols.length - 2] ?? "";
    const m = addr.match(/:(\d+)$/);
    if (!m || !Number.isInteger(pid)) continue;
    const port = Number(m[1]);
    if (!byPort.has(port)) byPort.set(port, { port, pid, name });
  }
  return [...byPort.values()].sort((x, y) => x.port - y.port);
}

/** Parse `tasklist /FO CSV /NH` → pid → image name. */
export function parseTasklist(csv: string): Record<number, string> {
  const map: Record<number, string> = {};
  for (const line of csv.split(/\r?\n/)) {
    const m = line.match(/^"([^"]+)","(\d+)"/);
    if (m) map[Number(m[2])] = m[1]!;
  }
  return map;
}

/** Parse a `a-b` (or `a:b`) range → [min, max], clamped to 1..65535. Falls back on garbage. */
export function parseRange(s: string, fallback: [number, number]): [number, number] {
  const m = String(s).match(/(\d+)\s*[-:]\s*(\d+)/);
  if (!m) return fallback;
  let a = Number(m[1]);
  let b = Number(m[2]);
  if (a > b) [a, b] = [b, a];
  return [Math.max(1, a), Math.min(65535, b)];
}

function toPort(v: string | undefined): number | undefined {
  if (v === undefined) return undefined;
  const n = Number(String(v).trim().replace(/^:/, ""));
  return Number.isInteger(n) && n > 0 && n <= 65535 ? n : undefined;
}

/** The ports THIS project serves on, parsed from its detected endpoints. */
export function endpointPorts(manifest: { endpoints: { url: string }[] }): number[] {
  const set = new Set<number>();
  for (const ep of manifest.endpoints) {
    const m = ep.url.match(/:(\d+)(?:\/|$)/);
    if (m) set.add(Number(m[1]));
    else if (/^https:/i.test(ep.url)) set.add(443);
    else if (/^http:/i.test(ep.url)) set.add(80);
  }
  return [...set].sort((a, b) => a - b);
}

function pickEnvFile(root: string): string {
  for (const f of [".env.local", ".env"]) if (existsSync(join(root, f))) return join(root, f);
  return join(root, ".env");
}

/** Set/replace the PORT= line in an env file (never touches any other value). */
export function upsertEnvPort(content: string, port: number): string {
  if (/^\s*PORT\s*=.*$/m.test(content)) return content.replace(/^\s*PORT\s*=.*$/m, `PORT=${port}`);
  return (content && !content.endsWith("\n") ? content + "\n" : content) + `PORT=${port}\n`;
}

/* ── free-port finder (node:net bind test) ─────────────────────────────────────── */

function isFree(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once("error", () => resolve(false));
    server.once("listening", () => server.close(() => resolve(true)));
    server.listen(port, "127.0.0.1");
  });
}

export async function findFreePort(min: number, max: number, tries = 60): Promise<number | undefined> {
  const span = Math.max(1, max - min + 1);
  for (let i = 0; i < tries; i++) {
    const port = min + Math.floor(Math.random() * span);
    if (await isFree(port)) return port;
  }
  return undefined;
}
