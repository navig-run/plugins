/**
 * env-doctor — keeps your local env in sync with the project's example. It auto-activates when the
 * project ships an example-env file (`.env.example` / `.env.local.example` / `.dev.vars.example` /
 * `.sample` / `.template`), shows how many documented keys are still missing from your real env, and
 * can scaffold the gaps. It works purely on variable NAMES — a value is never read out, printed, or
 * written (the scaffold adds empty `KEY=` lines). See docs/env-doctor-plugin.md.
 */

import { definePlugin } from "../../types.js";
import type { PluginContribution, PluginContext } from "../../types.js";
import { findEnvPairs } from "./config.js";
import { envDoctorBannerLines } from "./banner.js";
import * as h from "./handlers.js";

function detect(ctx: PluginContext): boolean {
  return findEnvPairs(ctx.root).length > 0;
}

function contribute(ctx: PluginContext): PluginContribution {
  return {
    settings: [{ key: "banner", label: "Env · missing-keys banner", type: "toggle", default: true }],
    bannerLines: ctx.settings.banner !== false ? envDoctorBannerLines(ctx) : [],
    sections: [
      {
        group: "Env",
        meta: { title: "ENV", emoji: "🔑", unicode: "◇", ascii: "k", tone: "green" },
        actions: [
          { id: "env.check", label: "Check env keys", internal: "check", description: "which documented keys are missing from your env (names only)" },
          { id: "env.scaffold", label: "Scaffold missing keys", internal: "scaffold", risk: "confirm", description: "append empty KEY= lines for the missing keys" },
        ],
      },
    ],
    about: ["env-doctor — compares example vs local env by key NAME only (never reads values)"],
  };
}

export const envDoctor = definePlugin({
  id: "env-doctor",
  tier: "programmatic",
  version: "1.0.0",
  detect,
  contribute,
  handlers: {
    check: h.check,
    scaffold: h.scaffold,
  },
});
