import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

import { resolveLauncher } from "../util/which.js";

/**
 * Detect whether NAVIG is available so we can offer relay mode (run commands / AI builds via
 * navig). Cheap + non-blocking: forced flag → live gateway → `navig` on PATH.
 */
export interface NavigPresence {
  present: boolean;
  gateway: boolean;
  forced: boolean;
}

const GATEWAY_FALLBACK = "http://127.0.0.1:8789";

/**
 * The gateway's self-healing bind can land it off the default port (Windows
 * reserves whole port ranges) — it records the live URL in
 * `~/.navig/gateway.json`. Read that first; fall back to the default.
 */
function gatewayBase(): string {
  try {
    const home = process.env.NAVIG_CONFIG_DIR || join(homedir(), ".navig");
    const disc = JSON.parse(readFileSync(join(home, "gateway.json"), "utf8")) as { url?: string };
    if (typeof disc?.url === "string" && /^https?:\/\/[^/]+$/.test(disc.url)) {
      return disc.url.replace(/\/+$/, "");
    }
  } catch {
    // no discovery file — daemon not started, or older core
  }
  return GATEWAY_FALLBACK;
}

export async function detectNavig(): Promise<NavigPresence> {
  const forced =
    process.env.NAVIG_MENU_RELAY === "1" || process.argv.includes("--relay");

  let gateway = false;
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 200);
    const res = await fetch(`${gatewayBase()}/health`, { signal: ctrl.signal });
    clearTimeout(t);
    gateway = res.ok;
  } catch {
    gateway = false;
  }

  const onPath = resolveLauncher("navig") !== "navig";
  return { present: forced || gateway || onPath, gateway, forced };
}
