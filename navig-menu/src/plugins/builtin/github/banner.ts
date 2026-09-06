/**
 * Banner stat line for the GitHub plugin. The repo (owner/name) is read offline from `.git/config`;
 * live numbers (stars, open PRs, CI status) are shown only from the plugin's own cache — populated by
 * the "Refresh stats" handler — so the banner never makes a network call or blocks the menu.
 */

import type { BannerLine, PluginContext } from "../../types.js";
import { loadGithubRepo } from "./config.js";

export interface GithubStatsCache {
  stars?: number;
  openPRs?: number;
  /** A gh run conclusion ("success"/"failure"/…) or in-progress status. */
  ci?: string;
  defaultBranch?: string;
  fetchedAt?: number;
}

export function githubBannerLines(ctx: PluginContext): BannerLine[] {
  const repo = loadGithubRepo(ctx.root);
  if (!repo) return [];

  const bits: string[] = [`${repo.owner}/${repo.repo}`];
  const stats = ctx.readCache<GithubStatsCache>("stats");
  if (stats) {
    if (typeof stats.stars === "number") bits.push(`★${stats.stars}`);
    if (typeof stats.openPRs === "number") bits.push(`${stats.openPRs} PR${stats.openPRs === 1 ? "" : "s"}`);
    if (stats.ci) bits.push(`CI ${ciGlyph(stats.ci)}`);
  }
  return [{ text: `GitHub · ${bits.join(" · ")}`, tone: "purple" }];
}

export function ciGlyph(ci: string): string {
  switch (ci) {
    case "success":
      return "✓";
    case "failure":
    case "cancelled":
    case "timed_out":
      return "✗";
    case "in_progress":
    case "queued":
    case "waiting":
      return "…";
    default:
      return ci;
  }
}
