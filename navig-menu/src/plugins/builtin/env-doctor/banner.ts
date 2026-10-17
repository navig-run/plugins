/**
 * Banner line for env-doctor: how many example keys are still missing from your local env — across
 * every example→target pair. Green when everything's set, yellow when keys are missing. Reads key
 * NAMES only (never values), offline.
 */

import type { BannerLine, PluginContext } from "../../types.js";
import { findEnvPairs, auditEnv } from "./config.js";

export function envDoctorBannerLines(ctx: PluginContext): BannerLine[] {
  const pairs = findEnvPairs(ctx.root);
  if (!pairs.length) return [];

  let missing = 0;
  for (const p of pairs) missing += auditEnv(ctx.root, p).missing.length;

  if (missing === 0) return [{ text: "env · all keys set", tone: "green" }];
  return [{ text: `env · ${missing} key${missing === 1 ? "" : "s"} missing`, tone: "yellow" }];
}
