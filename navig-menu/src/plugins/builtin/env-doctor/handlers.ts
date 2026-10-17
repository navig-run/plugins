/**
 * env-doctor handlers — the only place with side effects (reading env files, prompting, writing the
 * scaffold). SECURITY: only key NAMES are ever read, shown, or written; a value is never printed and
 * the scaffold writes empty `KEY=` lines. `check` is read-only; `scaffold` confirms before appending.
 */

import { writeFileSync } from "node:fs";
import { join } from "node:path";
import type { PluginActionContext } from "../../types.js";
import { readText } from "../../../detectors/fs.js";
import { findEnvPairs, auditEnv, type EnvPair } from "./config.js";
import { buildEnvAppend } from "./engine/dotenv.js";

/** Read-only audit: for each example→target pair, list which key names are present vs missing. */
export async function check(a: PluginActionContext): Promise<void> {
  const pairs = findEnvPairs(a.root);
  if (!pairs.length) return a.notify("no example env file found (.env.example / .dev.vars.example / …)");
  const c = a.theme.c;
  let totalMissing = 0;

  for (const pair of pairs) {
    const audit = auditEnv(a.root, pair);
    totalMissing += audit.missing.length;
    console.log("\n  " + c.white(pair.example) + c.dim(` → ${pair.target}${audit.targetExists ? "" : "  (not created yet)"}`));
    if (audit.missing.length === 0) {
      console.log("  " + c.green(`✓ all ${audit.exampleKeys.length} key${audit.exampleKeys.length === 1 ? "" : "s"} present`));
    } else {
      console.log("  " + c.yellow(`${audit.missing.length} missing:`));
      for (const k of audit.missing) console.log("    " + c.yellow("✗ ") + c.white(k));
    }
    if (audit.extra.length) {
      console.log("  " + c.dim(`${audit.extra.length} extra key${audit.extra.length === 1 ? "" : "s"} in ${pair.target} not in the example`));
    }
  }
  a.notify(totalMissing ? `${totalMissing} key(s) missing — run "Scaffold missing keys"` : "all env keys present");
}

/** Append empty `KEY=` lines for the missing keys to the target env file (confirmed; values never written). */
export async function scaffold(a: PluginActionContext): Promise<void> {
  const pairs = findEnvPairs(a.root);
  if (!pairs.length) return a.notify("no example env file found");

  let pair: EnvPair | undefined = pairs[0];
  if (pairs.length > 1) {
    const picked = await a.select({
      message: "Which env file?",
      choices: pairs.map((p) => ({ name: p.target, message: `${p.example} → ${p.target}` })),
    });
    if (picked === undefined) return a.notify("cancelled");
    pair = pairs.find((p) => p.target === picked);
  }
  if (!pair) return a.notify("cancelled");

  const audit = auditEnv(a.root, pair);
  if (!audit.missing.length) return a.notify(`${pair.target} already has every key from ${pair.example}`);

  const c = a.theme.c;
  console.log("\n  " + c.yellow(`about to add ${audit.missing.length} empty key(s) to ${pair.target}:`));
  for (const k of audit.missing) console.log("    " + c.white(k + "="));
  if (!(await a.confirm(`Append ${audit.missing.length} empty KEY= line(s) to ${pair.target}? (you fill in the values)`))) {
    return a.notify("cancelled");
  }

  const path = join(a.root, pair.target);
  writeFileSync(path, buildEnvAppend(readText(path) ?? "", audit.missing), "utf8");
  a.notify(`added ${audit.missing.length} key(s) to ${pair.target} — fill in the values`);
}
