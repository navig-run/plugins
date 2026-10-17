import { join } from "node:path";
import type { DetectContext } from "./context.js";
import { readYaml, readJson } from "./fs.js";
import type { WorkspaceInfo, Evidence } from "../manifest/schema.js";

/**
 * Workspace shape, in declaration-strength order:
 *  - pnpm-workspace.yaml / package.json `workspaces` (npm/yarn) / turbo.json / nx.json
 *  - "pseudo": a root that orchestrates sub-projects purely through scripts (no workspace cfg)
 *  - "none"
 */
export function detectWorkspace(ctx: DetectContext): WorkspaceInfo {
  const evidence: Evidence[] = [];
  const packages: string[] = [];

  const pnpmWs = readYaml<{ packages?: string[] }>(join(ctx.root, "pnpm-workspace.yaml"));
  if (pnpmWs) {
    if (Array.isArray(pnpmWs.packages)) packages.push(...pnpmWs.packages);
    return {
      kind: "pnpm",
      confidence: "explicit",
      packages,
      evidence: [{ kind: "file", file: "pnpm-workspace.yaml" }],
    };
  }

  const ws = ctx.pkg?.workspaces;
  if (ws) {
    const globs = Array.isArray(ws) ? ws : (ws.packages ?? []);
    packages.push(...globs);
    return {
      kind: "npm",
      confidence: "explicit",
      packages,
      evidence: [{ kind: "field", file: "package.json", detail: "workspaces" }],
    };
  }

  if (ctx.hasFile("turbo.json") || ctx.absExists("turbo.json")) {
    evidence.push({ kind: "file", file: "turbo.json" });
  }
  if (ctx.hasFile("nx.json") || ctx.absExists("nx.json")) {
    evidence.push({ kind: "file", file: "nx.json" });
  }
  if (evidence.length) {
    const turbo = evidence.some((e) => e.file === "turbo.json");
    return {
      kind: turbo ? "turbo" : "nx",
      confidence: "detected",
      packages,
      evidence,
    };
  }

  // Pseudo-monorepo heuristic: scripts that `cd` into sibling dirs or call sub-installs.
  const scripts = ctx.pkg?.scripts ?? {};
  const orchestrates = Object.values(scripts).some((s) =>
    /(^|&&|\s)(cd|--prefix|--cwd)\s/.test(s) || /\b(install:all|dev:all|build:all)\b/.test(s),
  );
  const siblingPkgs = ctx.scan.files.filter(
    (f) => /^[^/]+\/package\.json$/.test(f.rel),
  ).length;
  if (orchestrates && siblingPkgs >= 1) {
    return {
      kind: "pseudo",
      confidence: "inferred",
      packages,
      evidence: [{ kind: "script", file: "package.json", detail: "script-orchestrated sub-projects" }],
    };
  }

  // Nested workspace: a child dir declares its own workspace even though root doesn't.
  const nestedPnpm = ctx.scan.files.find((f) => /\/pnpm-workspace\.yaml$/.test(f.rel));
  if (nestedPnpm) {
    return {
      kind: "nested",
      confidence: "inferred",
      packages,
      evidence: [{ kind: "file", file: nestedPnpm.rel }],
    };
  }

  return { kind: "none", confidence: ctx.pkg ? "detected" : "unknown", packages, evidence: [] };
}

/** Resolve workspace package directories (best-effort, for `--deep` enumeration). */
export function resolveWorkspacePackages(ctx: DetectContext, ws: WorkspaceInfo): string[] {
  const dirs: string[] = [];
  for (const glob of ws.packages) {
    // Only handle the common `dir/*` and `dir` forms — no full glob engine needed.
    const star = glob.endsWith("/*");
    const base = star ? glob.slice(0, -2) : glob;
    const baseAbs = join(ctx.root, base);
    if (star) {
      const seen = new Set<string>();
      for (const f of ctx.scan.files) {
        const m = f.rel.match(new RegExp(`^${escapeRe(base)}/([^/]+)/package\\.json$`));
        if (m && !seen.has(m[1]!)) {
          seen.add(m[1]!);
          dirs.push(join(baseAbs, m[1]!));
        }
      }
    } else if (readJson(join(baseAbs, "package.json"))) {
      dirs.push(baseAbs);
    }
  }
  return dirs;
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
