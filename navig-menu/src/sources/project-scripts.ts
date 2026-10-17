import type { DetectContext } from "../detectors/context.js";
import type { MenuSource, SourceFinding } from "./index.js";
import type { Action, Framework, Evidence } from "../manifest/schema.js";
import { resolveCanonical } from "../builder/canonical.js";
import { riskFor } from "../builder/risk.js";
import { groupFor, isLongRunning } from "../builder/classify.js";

type PM = "npm" | "pnpm" | "yarn" | "bun" | "none";

/** Turns package.json scripts (+ non-JS ecosystem defaults) into runnable actions. */
export class ProjectScriptsSource implements MenuSource {
  readonly id = "project-scripts";
  constructor(
    private readonly pm: PM,
    private readonly frameworks: Framework[],
  ) {}

  detect(ctx: DetectContext): SourceFinding {
    const rawScripts = ctx.pkg?.scripts ?? {};
    const actions: Action[] = [];
    const scripts: Record<string, string> = {};
    const launcher = this.pm === "none" ? "npm" : this.pm;

    for (const [name, command] of Object.entries(rawScripts)) {
      // Skip visual separators / placeholders (e.g. `"——— DEV ———": ""`) — they are not runnable.
      if (!command || !command.trim() || isSeparator(name)) continue;
      // Skip the menu's own launcher scripts — running the menu from inside the menu is noise.
      if (isSelfMenu(name, command)) continue;
      scripts[name] = command;
      const canonical = resolveCanonical(name);
      actions.push({
        id: name,
        canonical,
        label: humanize(name),
        group: groupFor(name, canonical),
        launcher,
        argv: ["run", name],
        cwd: ".",
        risk: riskFor(name, command, canonical),
        longRunning: isLongRunning(name, command, canonical),
        confidence: "detected",
        why: `package.json script "${name}"`,
        evidence: [{ kind: "script", file: "package.json", detail: name }],
        source: this.id,
      });
    }

    // Ecosystem defaults for non-JS stacks, only for canonicals not already covered by a script.
    const covered = new Set(actions.map((a) => a.canonical).filter(Boolean) as string[]);
    actions.push(...ecosystemActions(ctx, this.frameworks, covered));

    return { actions, scripts };
  }
}

function ecosystemActions(
  ctx: DetectContext,
  frameworks: Framework[],
  covered: Set<string>,
): Action[] {
  const out: Action[] = [];
  const has = (id: string) => frameworks.some((f) => f.id === id);
  const add = (
    canonical: string,
    label: string,
    launcher: string,
    argv: string[],
    evidence: Evidence[],
    risk: Action["risk"] = "safe",
    longRunning = false,
  ) => {
    if (covered.has(canonical)) return;
    covered.add(canonical);
    out.push({
      id: `${launcher}:${canonical}`,
      canonical,
      label,
      group: groupFor(canonical, canonical as never),
      launcher,
      argv,
      cwd: ".",
      risk,
      longRunning,
      confidence: "inferred",
      why: `${launcher} ecosystem default`,
      evidence,
      source: "project-scripts",
    });
  };

  if (has("rust")) {
    const ev: Evidence[] = [{ kind: "file", file: "Cargo.toml" }];
    add("dev", "Run (cargo)", "cargo", ["run"], ev, "safe", true);
    add("build", "Build (cargo)", "cargo", ["build"], ev, "confirm");
    add("test", "Test (cargo)", "cargo", ["test"], ev);
  }
  if (has("go")) {
    const ev: Evidence[] = [{ kind: "file", file: "go.mod" }];
    add("build", "Build (go)", "go", ["build", "./..."], ev, "confirm");
    add("test", "Test (go)", "go", ["test", "./..."], ev);
  }
  if (has("python")) {
    const ev: Evidence[] = [{ kind: "file", file: "pyproject.toml" }];
    add("test", "Test (pytest)", "python", ["-m", "pytest"], ev);
  }
  if (has("laravel")) {
    const ev: Evidence[] = [{ kind: "file", file: "artisan" }];
    add("dev", "Serve (artisan)", "php", ["artisan", "serve"], ev, "safe", true);
    add("migrate", "Migrate (artisan)", "php", ["artisan", "migrate"], ev, "confirm");
  }
  return out;
}

/** A separator key is purely punctuation/spaces (the `"——— SECTION ———"` package.json trick). */
function isSeparator(name: string): boolean {
  return !/[a-z0-9]/i.test(name);
}

/** Scripts that just (re)launch a menu — this tool, or a sibling `scripts/menu.*` — are self-referential. */
function isSelfMenu(name: string, command: string): boolean {
  if (/^(menu|nm)(:.*)?$/i.test(name)) return true;
  return /(navig-menu|scripts[/\\]menu\.(mjs|cjs|js|ts|py))/i.test(command);
}

function humanize(name: string): string {
  return name
    .split(/[:_-]/)
    .map((p) => (p ? p[0]!.toUpperCase() + p.slice(1) : p))
    .join(" ");
}
