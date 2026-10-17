/**
 * Worker vs Pages classification + a binding-count summary. Config alone is not always enough:
 * many older Pages projects declare no `pages_build_output_dir` and are only recognizable from a
 * `wrangler pages …` script. A project with a `main` entrypoint is a Worker (even one using
 * `[assets]` for static files), so `main` wins over a stray pages script.
 */

import type { WranglerConfig, WranglerType } from "../config.js";

export function classifyType(config: WranglerConfig | undefined, scripts: Record<string, string> = {}): WranglerType {
  if (config?.pagesBuildOutputDir) return "pages";
  const usesPages = Object.values(scripts).some(
    (c) => /wrangler\s+pages\b/.test(c) || /(^|\s)pages:(deploy|dev|build)\b/.test(c),
  );
  // A Worker entrypoint is decisive; without one, a pages script implies a Pages project.
  if (usesPages && !config?.main) return "pages";
  return config?.type ?? "worker";
}

export interface BindingSummary {
  d1: number;
  kv: number;
  r2: number;
  do: number;
  queues: number;
  ai: boolean;
  vectorize: number;
  hyperdrive: number;
  services: number;
  sendEmail: number;
  assets: boolean;
}

export function summarize(config: WranglerConfig): BindingSummary {
  return {
    d1: config.d1.length,
    kv: config.kv.length,
    r2: config.r2.length,
    do: config.durableObjects.length,
    queues: config.queues.producers.length + config.queues.consumers.length,
    ai: config.ai,
    vectorize: config.vectorize.length,
    hyperdrive: config.hyperdrive.length,
    services: config.services.length,
    sendEmail: config.sendEmail.length,
    assets: config.assets,
  };
}
