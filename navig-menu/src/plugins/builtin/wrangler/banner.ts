/**
 * Banner stat line for the Cloudflare plugin — a pure, offline read of the parsed wrangler config
 * (worker/pages name, type, declared bindings, environments). An optional second line shows the
 * signed-in Cloudflare account, read only from the plugin's own cache (never a live network call in
 * the banner) and only when the `whoami` setting is on.
 */

import type { BannerLine, PluginContext } from "../../types.js";
import { loadWranglerConfig } from "./config.js";
import { classifyType, summarize } from "./engine/classify.js";

export interface WhoamiCache {
  account?: string;
  email?: string;
  fetchedAt?: number;
}

export function wranglerBannerLines(ctx: PluginContext): BannerLine[] {
  const config = loadWranglerConfig(ctx.root);
  if (!config) return [];

  const type = classifyType(config, ctx.scripts);
  const s = summarize(config);
  const parts = [
    s.d1 && `D1×${s.d1}`,
    s.kv && `KV×${s.kv}`,
    s.r2 && `R2×${s.r2}`,
    s.do && `DO×${s.do}`,
    s.ai && "AI",
    s.vectorize && `Vectorize×${s.vectorize}`,
    s.hyperdrive && `Hyperdrive×${s.hyperdrive}`,
    s.queues && `Queues×${s.queues}`,
    s.services && `svc×${s.services}`,
    s.assets && "assets",
  ].filter(Boolean) as string[];

  let line = `Cloudflare · ${config.name ?? "(unnamed)"} · ${type}`;
  if (parts.length) line += ` · ${parts.join(" · ")}`;
  if (config.envs.length) line += ` · env: ${config.envs.join("/")}`;

  const lines: BannerLine[] = [{ text: line, tone: "yellow" }];

  if (ctx.settings.whoami) {
    const who = ctx.readCache<WhoamiCache>("whoami");
    const acct = who?.email ?? who?.account;
    if (acct) lines.push({ text: `Cloudflare · signed in as ${acct}`, tone: "dim" });
  }
  return lines;
}
