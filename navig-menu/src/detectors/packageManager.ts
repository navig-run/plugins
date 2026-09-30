import type { DetectContext } from "./context.js";
import type { PackageManagerInfo, Evidence, Warning } from "../manifest/schema.js";

type PM = "npm" | "pnpm" | "yarn" | "bun";

const LOCKFILES: Record<PM, string> = {
  pnpm: "pnpm-lock.yaml",
  npm: "package-lock.json",
  yarn: "yarn.lock",
  bun: "bun.lockb",
};
// Resolution order when multiple lockfiles coexist (pnpm wins, bun last).
const PRIORITY: PM[] = ["pnpm", "yarn", "bun", "npm"];

export interface PmResult {
  info: PackageManagerInfo;
  warnings: Warning[];
}

/**
 * Priority: `packageManager` field → lockfile → (workspace config handled by caller) →
 * default npm if a package.json exists → none. Conflicting lockfiles produce a warning and
 * drop confidence to `inferred`.
 */
export function detectPackageManager(ctx: DetectContext): PmResult {
  const warnings: Warning[] = [];

  // 1. Explicit `packageManager` field — unambiguous.
  const field = ctx.pkg?.packageManager;
  if (field) {
    const name = field.split("@")[0]?.toLowerCase() as PM | undefined;
    if (name && name in LOCKFILES) {
      return {
        info: {
          value: name,
          confidence: "explicit",
          evidence: [{ kind: "field", file: "package.json", detail: `packageManager=${field}` }],
        },
        warnings,
      };
    }
  }

  // 2. Lockfiles present.
  const present = PRIORITY.filter((pm) => ctx.hasFile(LOCKFILES[pm]) || ctx.absExists(LOCKFILES[pm]));
  // bun.lock (text) is a newer alternative to bun.lockb.
  if (!present.includes("bun") && (ctx.hasFile("bun.lock") || ctx.absExists("bun.lock"))) {
    present.push("bun");
  }

  if (present.length === 1) {
    const pm = present[0]!;
    return {
      info: {
        value: pm,
        confidence: "detected",
        evidence: [{ kind: "lockfile", file: LOCKFILES[pm] }],
      },
      warnings,
    };
  }

  if (present.length > 1) {
    const chosen = present[0]!; // PRIORITY-ordered
    warnings.push({
      code: "multiple_lockfiles",
      detail: present.map((p) => LOCKFILES[p]).join(" + "),
    });
    return {
      info: {
        value: chosen,
        confidence: "inferred",
        evidence: present.map<Evidence>((p) => ({ kind: "lockfile", file: LOCKFILES[p] })),
      },
      warnings,
    };
  }

  // 3. No lockfile — npm if there's a package.json at all, else none.
  if (ctx.pkg) {
    return {
      info: {
        value: "npm",
        confidence: "inferred",
        evidence: [{ kind: "file", file: "package.json", detail: "no lockfile; defaulting to npm" }],
      },
      warnings,
    };
  }

  return { info: { value: "none", confidence: "unknown", evidence: [] }, warnings };
}
