/**
 * GitHub pack — a command palette for the project's GitHub repo, driven by the `gh` CLI. It
 * auto-activates in any repo whose `.git/config` has a GitHub remote (github.com or Enterprise),
 * shows an owner/repo banner (with cached stars / open-PR / CI numbers once refreshed), and surfaces
 * PRs, issues, Actions/CI, releases, and repo/auth views. `gh` owns its own auth, so no credentials
 * pass through the plugin; every action runs as an argv array (no shell). See docs/github-plugin.md.
 */

import { definePlugin } from "../../types.js";
import type { PluginActionSpec, PluginContribution, PluginContext } from "../../types.js";
import { loadGithubRepo } from "./config.js";
import { githubBannerLines } from "./banner.js";
import * as h from "./handlers.js";

function detect(ctx: PluginContext): boolean {
  return loadGithubRepo(ctx.root) !== undefined;
}

function contribute(ctx: PluginContext): PluginContribution {
  const repo = loadGithubRepo(ctx.root);
  const actions: PluginActionSpec[] = [
    { id: "gh.status", label: "Status", internal: "status", description: "your PRs + the current-branch PR (gh pr status)" },
    { id: "gh.pr", label: "Pull requests…", internal: "pullRequests", description: "list · view · check out · create · open" },
    { id: "gh.issues", label: "Issues…", internal: "issues", description: "list · view · create · open" },
    { id: "gh.actions", label: "Actions / CI…", internal: "actions", description: "runs · view · log · watch · re-run failed" },
    { id: "gh.releases", label: "Releases…", internal: "releases", risk: "confirm", description: "list · create a release (publishes)" },
    { id: "gh.repo", label: "Repo overview", internal: "repoView", description: "gh repo view" },
    { id: "gh.web", label: "Open on GitHub", internal: "openWeb", description: "open the repo in your browser" },
    { id: "gh.refresh", label: "Refresh stats", internal: "refreshStats", description: "stars · open PRs · CI → banner" },
    { id: "gh.auth", label: "Auth status", internal: "authStatus", description: "which GitHub account gh uses" },
  ];

  return {
    settings: [{ key: "banner", label: "GitHub · banner stat line", type: "toggle", default: true }],
    bannerLines: ctx.settings.banner !== false ? githubBannerLines(ctx) : [],
    sections: [
      {
        group: "GitHub",
        meta: { title: "GITHUB", emoji: "🐙", unicode: "◆", ascii: "g", tone: "purple" },
        actions,
      },
    ],
    about: [
      repo
        ? `github plugin — ${repo.owner}/${repo.repo} via the gh CLI`
        : "github plugin — GitHub repo operations (PRs · issues · CI · releases) via the gh CLI",
    ],
  };
}

export const github = definePlugin({
  id: "github",
  tier: "programmatic",
  version: "1.0.0",
  detect,
  contribute,
  handlers: {
    status: h.status,
    pullRequests: h.pullRequests,
    issues: h.issues,
    actions: h.actions,
    releases: h.releases,
    repoView: h.repoView,
    openWeb: h.openWeb,
    refreshStats: h.refreshStats,
    authStatus: h.authStatus,
  },
});
