import { describe, it, expect } from "vitest";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  parseGithubRemote,
  parseGitConfigRemotes,
  githubRemoteFromConfig,
  isGithubHost,
} from "../src/plugins/builtin/github/engine/remote.js";
import * as cmd from "../src/plugins/builtin/github/engine/command.js";
import { loadGithubRepo } from "../src/plugins/builtin/github/config.js";
import { githubBannerLines, ciGlyph } from "../src/plugins/builtin/github/banner.js";
import { github } from "../src/plugins/builtin/github/index.js";
import type { PluginContext } from "../src/plugins/types.js";

/* ── remote URL parsing ────────────────────────────────────────────────────────────── */

describe("parseGithubRemote", () => {
  it("parses https, ssh, and scp-like URLs (and strips .git)", () => {
    expect(parseGithubRemote("https://github.com/navig-run/labs.git")).toEqual({ owner: "navig-run", repo: "labs", host: "github.com" });
    expect(parseGithubRemote("https://github.com/navig-run/labs")).toEqual({ owner: "navig-run", repo: "labs", host: "github.com" });
    expect(parseGithubRemote("git@github.com:navig-run/labs.git")).toEqual({ owner: "navig-run", repo: "labs", host: "github.com" });
    expect(parseGithubRemote("ssh://git@github.com/navig-run/labs.git")).toEqual({ owner: "navig-run", repo: "labs", host: "github.com" });
  });

  it("keeps the enterprise host", () => {
    expect(parseGithubRemote("git@github.acme.com:team/app.git")).toEqual({ owner: "team", repo: "app", host: "github.acme.com" });
  });

  it("returns undefined for junk / non-repo URLs", () => {
    expect(parseGithubRemote("")).toBeUndefined();
    expect(parseGithubRemote("https://github.com/justowner")).toBeUndefined();
    expect(parseGithubRemote("not a url")).toBeUndefined();
  });
});

describe("isGithubHost", () => {
  it("recognizes github.com and enterprise, rejects others", () => {
    expect(isGithubHost("github.com")).toBe(true);
    expect(isGithubHost("github.acme.com")).toBe(true);
    expect(isGithubHost("gitlab.com")).toBe(false);
    expect(isGithubHost("bitbucket.org")).toBe(false);
  });
});

describe("parseGitConfigRemotes + githubRemoteFromConfig", () => {
  const CONFIG = `[core]
	repositoryformatversion = 0
[remote "upstream"]
	url = https://gitlab.com/other/mirror.git
[remote "origin"]
	url = git@github.com:navig-run/labs.git
	fetch = +refs/heads/*:refs/remotes/origin/*
[branch "main"]
	remote = origin
`;

  it("extracts every remote url", () => {
    expect(parseGitConfigRemotes(CONFIG)).toEqual({
      upstream: "https://gitlab.com/other/mirror.git",
      origin: "git@github.com:navig-run/labs.git",
    });
  });

  it("prefers the origin remote and requires a github host", () => {
    expect(githubRemoteFromConfig(CONFIG)).toEqual({ owner: "navig-run", repo: "labs", host: "github.com", remote: "origin" });
  });

  it("falls back to a non-origin github remote when origin isn't github", () => {
    const cfg = `[remote "origin"]\n\turl = https://gitlab.com/x/y.git\n[remote "gh"]\n\turl = https://github.com/a/b.git\n`;
    expect(githubRemoteFromConfig(cfg)).toEqual({ owner: "a", repo: "b", host: "github.com", remote: "gh" });
  });

  it("returns undefined when no remote is github", () => {
    expect(githubRemoteFromConfig(`[remote "origin"]\n\turl = https://gitlab.com/x/y.git\n`)).toBeUndefined();
  });
});

/* ── command builders ──────────────────────────────────────────────────────────────── */

describe("gh command builders", () => {
  it("pr list json with state + limit", () => {
    expect(cmd.prListJsonArgs(["number", "title"], { state: "open", limit: 30 })).toEqual([
      "pr", "list", "--json", "number,title", "--state", "open", "--limit", "30",
    ]);
  });
  it("pr view / checkout", () => {
    expect(cmd.prViewArgs("42", true)).toEqual(["pr", "view", "42", "--web"]);
    expect(cmd.prCheckoutArgs("42")).toEqual(["pr", "checkout", "42"]);
  });
  it("run view with log + rerun failed", () => {
    expect(cmd.runViewArgs("99", { log: true })).toEqual(["run", "view", "99", "--log"]);
    expect(cmd.runRerunArgs("99", true)).toEqual(["run", "rerun", "99", "--failed"]);
  });
  it("release create with generated notes", () => {
    expect(cmd.releaseCreateArgs("v1.2.0", { generateNotes: true })).toEqual(["release", "create", "v1.2.0", "--generate-notes"]);
  });
  it("repo view json", () => {
    expect(cmd.repoViewJsonArgs(["stargazerCount", "defaultBranchRef"])).toEqual(["repo", "view", "--json", "stargazerCount,defaultBranchRef"]);
  });
});

describe("ciGlyph", () => {
  it("maps conclusions to glyphs", () => {
    expect(ciGlyph("success")).toBe("✓");
    expect(ciGlyph("failure")).toBe("✗");
    expect(ciGlyph("in_progress")).toBe("…");
    expect(ciGlyph("weird")).toBe("weird");
  });
});

/* ── banner + end-to-end detect/contribute ─────────────────────────────────────────── */

const GH_CONFIG = `[remote "origin"]\n\turl = https://github.com/navig-run/labs.git\n`;

function makeCtx(root: string, cache?: unknown): PluginContext {
  return {
    root,
    manifest: { scripts: {} } as never,
    scripts: {},
    settings: {},
    cacheDir: join(root, ".cache"),
    hasFile: () => false,
    hasDep: () => false,
    readCache: () => cache,
  } as unknown as PluginContext;
}

describe("githubBannerLines", () => {
  it("shows owner/repo, plus cached stats when present", () => {
    const dir = mkdtempSync(join(tmpdir(), "gh-b-"));
    try {
      mkdirSync(join(dir, ".git"));
      writeFileSync(join(dir, ".git", "config"), GH_CONFIG);
      // no cache → just the repo
      expect(githubBannerLines(makeCtx(dir))[0]?.text).toBe("GitHub · navig-run/labs");
      // with cache → stars / PRs / CI
      const withStats = githubBannerLines(makeCtx(dir, { stars: 12, openPRs: 3, ci: "success" }));
      expect(withStats[0]?.text).toBe("GitHub · navig-run/labs · ★12 · 3 PRs · CI ✓");
      expect(withStats[0]?.tone).toBe("purple");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("returns [] for a non-github project", () => {
    const dir = mkdtempSync(join(tmpdir(), "gh-nob-"));
    try {
      expect(githubBannerLines(makeCtx(dir))).toEqual([]);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe("loadGithubRepo + plugin.detect/contribute", () => {
  it("detects a github repo from .git/config and contributes the rail", () => {
    const dir = mkdtempSync(join(tmpdir(), "gh-proj-"));
    try {
      mkdirSync(join(dir, ".git"));
      writeFileSync(join(dir, ".git", "config"), GH_CONFIG);
      expect(loadGithubRepo(dir)).toMatchObject({ owner: "navig-run", repo: "labs" });

      const ctx = makeCtx(dir);
      expect(github.detect?.(ctx)).toBe(true);
      const ids = (github.contribute(ctx).sections?.[0]?.actions ?? []).map((a) => a.id);
      expect(ids).toEqual(
        expect.arrayContaining(["gh.status", "gh.pr", "gh.issues", "gh.actions", "gh.releases", "gh.repo", "gh.web", "gh.refresh", "gh.auth"]),
      );
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("does not detect a non-git or non-github dir", () => {
    const dir = mkdtempSync(join(tmpdir(), "gh-none-"));
    try {
      expect(loadGithubRepo(dir)).toBeUndefined();
      expect(github.detect?.(makeCtx(dir))).toBe(false);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
