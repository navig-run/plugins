/**
 * Cloudflare / Wrangler pack — a config-aware command palette over the real `wrangler` CLI. It
 * auto-activates in any Cloudflare project (a `wrangler.toml|jsonc|json`, a `wrangler`/`@cloudflare/*`
 * dependency, or a `wrangler`-invoking script), parses that config offline, and surfaces exactly the
 * bindings the project declares — Workers vs Pages, D1 / KV / R2 / Durable Objects / Queues /
 * Vectorize / Hyperdrive / Workers AI / secrets / environments — plus deploy, dev, tail, typegen and
 * auth. Every action runs wrangler as an argv array (no shell); production/remote ops confirm first.
 * See docs/wrangler-plugin.md.
 */

import { definePlugin } from "../../types.js";
import type { PluginActionSpec, PluginContribution, PluginContext } from "../../types.js";
import { loadWranglerConfig, findWranglerConfig } from "./config.js";
import { classifyType } from "./engine/classify.js";
import { wranglerBannerLines } from "./banner.js";
import * as h from "./handlers.js";

function scriptsMention(scripts: Record<string, string>, re: RegExp): boolean {
  return Object.values(scripts).some((c) => re.test(c));
}

/** Detected when a wrangler config, a wrangler/@cloudflare dep, or a wrangler-invoking script exists. */
function detect(ctx: PluginContext): boolean {
  if (findWranglerConfig(ctx.root)) return true;
  if (ctx.hasDep("wrangler", "@cloudflare/")) return true;
  return scriptsMention(ctx.scripts, /\bwrangler\b|opennextjs-cloudflare/);
}

function contribute(ctx: PluginContext): PluginContribution {
  const config = loadWranglerConfig(ctx.root);
  const generic = !config; // config-less wrangler project → show the core rails anyway
  const type = classifyType(config, ctx.scripts);
  const has = (n: number | undefined, re: RegExp): boolean => generic || (n ?? 0) > 0 || scriptsMention(ctx.scripts, re);

  const actions: PluginActionSpec[] = [
    { id: "cf.dev", label: type === "pages" ? "Dev (pages dev)" : "Dev server", internal: "dev", longRunning: true, description: "run the worker/pages dev server locally" },
    { id: "cf.deploy", label: type === "pages" ? "Deploy (Pages)" : "Deploy", internal: "deploy", risk: "dangerous", longRunning: true, description: "publish to Cloudflare (asks for the environment)" },
    { id: "cf.tail", label: "Tail logs", internal: "tail", longRunning: true, description: "stream live logs from the deployed worker/pages" },
    { id: "cf.deployments", label: "Deployments / rollback", internal: "deployments", risk: "confirm", description: "list deployments & versions · roll back" },
  ];

  if (has(config?.d1.length, /\bd1\b/))
    actions.push({ id: "cf.d1", label: "D1 database…", internal: "d1", risk: "confirm", description: "query · execute file · migrations (local/remote) · export" });
  if (has(config?.kv.length, /\bkv\b/))
    actions.push({ id: "cf.kv", label: "KV storage…", internal: "kv", risk: "confirm", description: "namespaces · list/get/put/delete keys" });
  if (has(config?.r2.length, /\br2\b/))
    actions.push({ id: "cf.r2", label: "R2 storage…", internal: "r2", risk: "confirm", description: "buckets · upload/download/delete objects" });

  actions.push({ id: "cf.secrets", label: "Secrets…", internal: "secrets", risk: "confirm", description: "list / set / delete (values never leave wrangler)" });

  if ((config?.queues.producers.length ?? 0) + (config?.queues.consumers.length ?? 0) > 0)
    actions.push({ id: "cf.queues", label: "Queues…", internal: "queues", risk: "confirm", description: "list · create queues" });
  if ((config?.vectorize.length ?? 0) > 0)
    actions.push({ id: "cf.vectorize", label: "Vectorize indexes", internal: "vectorize", description: "list vectorize indexes" });
  if ((config?.hyperdrive.length ?? 0) > 0)
    actions.push({ id: "cf.hyperdrive", label: "Hyperdrive configs", internal: "hyperdrive", description: "list hyperdrive configs" });

  actions.push(
    { id: "cf.types", label: "Generate types", internal: "types", description: "regenerate binding types (wrangler types)" },
    { id: "cf.whoami", label: "Who am I", internal: "whoami", description: "the signed-in Cloudflare account (caches it for the banner)" },
    { id: "cf.login", label: "Login", internal: "login", description: "browser OAuth login (wrangler login)" },
  );

  // NB: we deliberately do NOT `claim` the repo's own wrangler scripts into this rail — the generated
  // cf.* actions already cover dev/deploy/tail (with env-awareness + confirmation), so claiming would
  // duplicate them (a bare "Deploy" next to "Deploy"). The repo's scripts stay in their normal rails.
  return {
    settings: [
      { key: "banner", label: "Cloudflare · banner stat line", type: "toggle", default: true },
      { key: "remoteConfirm", label: "Cloudflare · confirm before production/remote ops", type: "toggle", default: true },
      { key: "runner", label: "Cloudflare · wrangler runner", type: "cycle", values: ["auto", "npx", "pnpm", "bun"], default: "auto" },
      { key: "whoami", label: "Cloudflare · show account in banner", type: "toggle", default: false },
    ],
    bannerLines: ctx.settings.banner !== false ? wranglerBannerLines(ctx) : [],
    sections: [
      {
        group: "Cloudflare",
        meta: { title: "CLOUDFLARE", emoji: "☁️", unicode: "◈", ascii: "c", tone: "yellow" },
        actions,
      },
    ],
    about: [
      config
        ? `wrangler plugin — ${config.name ?? "worker"} (${type}) via ${config.file}`
        : "wrangler plugin — Cloudflare Workers/Pages command palette (add a wrangler.toml to unlock config-aware actions)",
    ],
  };
}

export const wrangler = definePlugin({
  id: "wrangler",
  tier: "programmatic",
  version: "1.0.0",
  detect,
  contribute,
  handlers: {
    dev: h.dev,
    deploy: h.deploy,
    tail: h.tail,
    deployments: h.deployments,
    d1: h.d1,
    kv: h.kv,
    r2: h.r2,
    secrets: h.secrets,
    queues: h.queues,
    vectorize: h.vectorize,
    hyperdrive: h.hyperdrive,
    types: h.types,
    whoami: h.whoami,
    login: h.login,
  },
});
