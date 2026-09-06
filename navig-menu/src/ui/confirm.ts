import type { Action } from "../manifest/schema.js";
import type { Theme } from "./theme.js";
import { confirmPrompt, input, CANCEL } from "./prompts.js";
import { aiAvailable, aiExplainAction } from "../runners/ai.js";

export interface ConfirmOptions {
  /** `--yes` skips the `confirm` tier. It NEVER skips the typed `dangerous` confirmation. */
  yes?: boolean;
  /** Explicit opt-in to bypass even the dangerous typed prompt (e.g. `--force-dangerous`). */
  forceDangerous?: boolean;
}

/** Render the command-review block — never prints secret values, only the argv we will run.
 *  A personal `note` (logins / important info) is surfaced prominently ABOVE the details. */
export function reviewBlock(action: Action, root: string, theme: Theme): string {
  const { c } = theme;
  const cmd = `${action.launcher} ${action.argv.join(" ")}`.trim();
  const riskColor =
    action.risk === "dangerous" ? c.red : action.risk === "confirm" ? c.yellow : c.green;

  const out: string[] = [""];
  if (action.note) {
    out.push("  " + c.bgYellow.black.bold(" NOTE "));
    for (const line of action.note.split("\n")) out.push("  " + c.yellowBright(line));
    out.push("");
  }
  out.push(
    `  ${c.dim("Action:")}  ${c.white(action.label)}`,
    `  ${c.dim("Risk:")}    ${riskColor(action.risk)}`,
    `  ${c.dim("Dir:")}     ${c.white(action.cwd === "." ? root : action.cwd)}`,
    `  ${c.dim("Command:")} ${c.cyan(cmd)}`,
  );
  if (action.why) out.push(`  ${c.dim("Why:")}     ${c.dim(action.why)}`);
  out.push("");
  return out.join("\n");
}

/**
 * Gate an action by its risk tier:
 *  - safe       → run
 *  - confirm    → one y/N (skipped by --yes)
 *  - dangerous  → red impact + TYPED confirmation; --yes cannot bypass (only --force-dangerous)
 */
export async function confirmRisk(
  action: Action,
  theme: Theme,
  opts: ConfirmOptions = {},
  root?: string,
): Promise<boolean> {
  const { c, sym } = theme;
  if (action.risk === "safe") return true;

  if (action.risk === "confirm") {
    if (opts.yes) return true;
    const ok = await confirmPrompt(`Run ${c.white(action.label)}?`, false);
    return ok === true;
  }

  // dangerous
  if (opts.forceDangerous) return true;

  // A plain-English AI safety review before the typed confirmation. High-stakes and rare, so we
  // run it automatically when a key is available — it directly serves "warn before destructive".
  if (root !== undefined && aiAvailable()) {
    process.stdout.write("  " + c.dim("AI safety review…") + "\n");
    const explanation = await aiExplainAction(action, root);
    if (explanation) {
      process.stdout.write(
        "  " + c.dim("What this does (AI summary — verify):") + "\n" +
        "  " + c.dim(explanation.replace(/\s*\n\s*/g, " ")) + "\n",
      );
    }
  }

  const token = action.canonical ?? action.id;
  process.stdout.write(
    "\n  " +
      c.bgRed.white.bold(" DANGEROUS ") +
      " " +
      c.red(`This may be irreversible (${action.label}).`) +
      "\n  " +
      c.dim(`Type "${token}" to proceed, or anything else to cancel.`) +
      "\n",
  );
  const typed = await input(`confirm ${sym.pointer}`);
  if (typed === CANCEL) return false;
  return typed.trim() === token;
}
