import { existsSync, readFileSync } from "node:fs";
import { join, isAbsolute, relative } from "node:path";
import type { Action } from "../manifest/schema.js";

/**
 * Post-mortem failure diagnosis. When an action exits non-zero we match its captured
 * stderr against a small, high-signal rule table and, when we can, propose a single
 * confirm-gated fix. Deterministic and offline — no LLM. It only speaks when it has
 * something actionable; unknown errors return `null` so the menu stays quiet.
 *
 * Philosophy (NAVIG): predictable, consent-gated, never silently mutate. Safe/idempotent
 * repairs (installing deps) get an offered fix; riskier ones (pip-global, killing a
 * port-holder) are advice-only.
 */

/** A single runnable repair the menu can offer to execute (always confirm-gated). */
export interface DiagnosisFix {
  /** Human label, e.g. "Install dependencies in tauri-ui/". */
  label: string;
  launcher: string;
  argv: string[];
  /** Absolute directory to run the fix in. */
  cwd: string;
}

export interface Diagnosis {
  title: string;
  hint: string;
  fix?: DiagnosisFix;
}

const PM_SET = new Set(["npm", "pnpm", "yarn", "bun"]);

function absCwd(action: Action, root: string): string {
  if (isAbsolute(action.cwd)) return action.cwd;
  return action.cwd === "." ? root : join(root, action.cwd);
}

function scriptBody(dir: string, name: string): string | null {
  try {
    const pkg = JSON.parse(readFileSync(join(dir, "package.json"), "utf8"));
    const v = pkg?.scripts?.[name];
    return typeof v === "string" ? v : null;
  } catch {
    return null;
  }
}

/**
 * Where the failure actually happened. npm/pnpm echo each nested script (`> pkg@v script`
 * and the raw command) to stderr, so a `cd <dir>` in a script chain is visible in the
 * captured text even when it's several scripts deep. Prefer that; fall back to parsing a
 * `cd` prefix out of the invoked script body; else the action's own cwd.
 */
function effectiveDir(action: Action, root: string, captured: string): string {
  const base = absCwd(action, root);

  // Primary: the deepest `cd <dir>` echoed in the actual run output. Tolerate the
  // leading `> ` that npm/pnpm print before each lifecycle command.
  const cds = [...captured.matchAll(/(?:^|\n|&&|;|\|)\s*>?\s*cd\s+["']?([^"'\s&|;<>]+)["']?/gi)];
  const lastCd = cds.at(-1)?.[1];
  if (lastCd) {
    const dir = isAbsolute(lastCd) ? lastCd : join(base, lastCd);
    if (existsSync(join(dir, "package.json")) || existsSync(dir)) return dir;
  }

  // Fallback: a `cd <dir> &&` prefix on the invoked npm script (no captured echo).
  if (PM_SET.has(action.launcher) && action.argv[0] === "run" && action.argv[1]) {
    const body = scriptBody(base, action.argv[1]);
    const sub = body && /^\s*cd\s+["']?([^"'\s&|;<>]+)["']?\s*&&/.exec(body)?.[1];
    if (sub) {
      const dir = join(base, sub);
      if (existsSync(join(dir, "package.json"))) return dir;
    }
  }

  return base;
}

/** Best-guess package manager for a fix run: the action's own launcher, else the lockfile. */
function pmFor(action: Action, dir: string): string {
  if (PM_SET.has(action.launcher)) return action.launcher;
  if (existsSync(join(dir, "pnpm-lock.yaml"))) return "pnpm";
  if (existsSync(join(dir, "yarn.lock"))) return "yarn";
  if (existsSync(join(dir, "bun.lockb")) || existsSync(join(dir, "bun.lock"))) return "bun";
  return "npm";
}

function relDir(root: string, dir: string): string {
  const r = relative(root, dir);
  return r === "" ? "." : r.replace(/\\/g, "/");
}

export function diagnose(
  action: Action,
  root: string,
  exitCode: number,
  captured: string,
): Diagnosis | null {
  if (exitCode === 0) return null;
  const text = captured ?? "";
  if (!text.trim()) return null;

  const dir = effectiveDir(action, root, text);
  const rel = relDir(root, dir);
  const pm = pmFor(action, dir);
  const hasPkg = existsSync(join(dir, "package.json"));
  const depsMissing = hasPkg && !existsSync(join(dir, "node_modules"));
  const installFix = (): DiagnosisFix => ({
    label: `Install dependencies in ${rel}/`,
    launcher: pm,
    argv: ["install"],
    cwd: dir,
  });

  // 1) A command isn't on PATH (Windows "not recognized", *nix "command not found").
  const notFound =
    /['"]?([\w.\-@/]+)['"]? is not recognized as an internal or external command/i.exec(text) ||
    /(?:sh|bash|zsh|env|cmd):.*?['"]?([\w.\-@/]+)['"]?:? (?:command )?not found/i.exec(text) ||
    /(?:^|\n)\s*([\w.\-@/]+): (?:command )?not found/m.exec(text);
  if (notFound) {
    const cmd = notFound[1];
    if (hasPkg) {
      return {
        title: `Missing command: ${cmd}`,
        hint:
          `"${cmd}" isn't on PATH. It's usually a local dependency exposed via ` +
          `node_modules/.bin — install deps in ${rel}/ so the binary exists` +
          (depsMissing ? " (node_modules is missing there)." : "."),
        fix: installFix(),
      };
    }
    return {
      title: `Missing command: ${cmd}`,
      hint: `"${cmd}" isn't installed or on PATH. Install it (as a project dependency, or globally) and retry.`,
    };
  }

  // 2) A module/package couldn't be resolved → dependencies aren't installed.
  if (/Cannot find (?:module|package)|ERR_MODULE_NOT_FOUND|MODULE_NOT_FOUND/i.test(text)) {
    return {
      title: "Dependencies not installed",
      hint: `A module couldn't be resolved — dependencies look missing in ${rel}/. Install them and retry.`,
      fix: hasPkg ? installFix() : undefined,
    };
  }

  // 3) The npm script itself doesn't exist.
  const missScript = /(?:npm error|npm ERR!)\s+Missing script:\s+["']?([\w:.\-]+)/i.exec(text);
  if (missScript) {
    return {
      title: `No such script: ${missScript[1]}`,
      hint: `package.json in ${rel}/ has no "${missScript[1]}" script. Run \`${pm} run\` to list what's available.`,
    };
  }

  // 4) Port already bound — advice only (killing the holder is risky).
  const port = /EADDRINUSE|address already in use|port\s+(\d+)\s+is (?:already )?in use/i.exec(text);
  if (port) {
    const p = port[1] ? ` ${port[1]}` : "";
    return {
      title: `Port${p} already in use`,
      hint: `Another process is holding the port${p}. Stop that process, or change the port in config, then retry.`,
    };
  }

  // 5) Python import failure — advice only (correct pip/venv target is ambiguous).
  const pyMod = /ModuleNotFoundError: No module named ['"]([\w.]+)['"]/.exec(text);
  if (pyMod?.[1]) {
    const mod = pyMod[1].split(".")[0];
    return {
      title: `Python module missing: ${mod}`,
      hint: `Python can't find "${mod}". Activate your virtualenv, then \`pip install ${mod}\` (or install the project's requirements).`,
    };
  }

  return null;
}
