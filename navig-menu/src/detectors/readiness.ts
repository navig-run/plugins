import { existsSync } from "node:fs";
import { join } from "node:path";
import type { Manifest } from "../manifest/schema.js";

/**
 * Proactive setup checks — is this project ready to run, or does it need a first-time step?
 * Deterministic and offline; surfaced before the menu opens and in `doctor`. Complements the
 * post-mortem `diagnose` (which reacts to a failure) by catching the common cases up front.
 */

export interface ReadinessFix {
  launcher: string;
  argv: string[];
  label: string;
}

export interface ReadinessIssue {
  kind: "deps" | "env" | "python-venv";
  message: string;
  /** An offered one-key fix (install deps). Absent for advice-only issues. */
  fix?: ReadinessFix;
}

const ENV_TEMPLATES = [".env.example", ".env.sample", ".env.template", ".env.dist"];

export function checkReadiness(root: string, manifest: Manifest): ReadinessIssue[] {
  const issues: ReadinessIssue[] = [];

  // Node dependencies not installed.
  if (existsSync(join(root, "package.json")) && !existsSync(join(root, "node_modules"))) {
    const pm = manifest.packageManager.value === "none" ? "npm" : manifest.packageManager.value;
    issues.push({
      kind: "deps",
      message: `Dependencies aren't installed (no node_modules).`,
      fix: { launcher: pm, argv: ["install"], label: `${pm} install` },
    });
  }

  // An env template is present but there's no real .env yet.
  if (!existsSync(join(root, ".env"))) {
    const template = ENV_TEMPLATES.find((t) => existsSync(join(root, t)));
    if (template) {
      issues.push({
        kind: "env",
        message: `Found ${template} but no .env — copy it and fill in the values.`,
      });
    }
  }

  // Python project with no virtualenv active/present.
  const hasPyDeps =
    existsSync(join(root, "requirements.txt")) || existsSync(join(root, "pyproject.toml"));
  const hasVenv =
    existsSync(join(root, ".venv")) || existsSync(join(root, "venv")) || !!process.env.VIRTUAL_ENV;
  if (hasPyDeps && !hasVenv) {
    issues.push({
      kind: "python-venv",
      message: "Python project without an active virtualenv — create one and install requirements.",
    });
  }

  return issues;
}
