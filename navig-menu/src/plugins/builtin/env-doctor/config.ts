/**
 * Discover example-env → target-env pairs in a project and audit which key NAMES are missing. An
 * "example" is any `.env[.*].example|sample|template` or `.dev.vars.example` (the Cloudflare Workers
 * convention); the target is the same name without that suffix (`.env.example`→`.env`,
 * `.dev.vars.example`→`.dev.vars`, `.env.local.example`→`.env.local`). All reads go through
 * `parseEnvKeys`, so only names — never values — are ever handled.
 */

import { readdirSync } from "node:fs";
import { join } from "node:path";
import { readText } from "../../../detectors/fs.js";
import { parseEnvKeys } from "./engine/dotenv.js";

/** An example-env file paired with the real env file it documents. */
export interface EnvPair {
  example: string;
  target: string;
}

const EXAMPLE_RE = /^(\.env(\.[^.]+)*|\.dev\.vars)\.(example|sample|template)$/i;

/** Strip the trailing `.example|.sample|.template` to get the real filename. */
export function exampleTarget(name: string): string {
  return name.replace(/\.(example|sample|template)$/i, "");
}

/** Find every example→target env pair at the project root (deduped by target). */
export function findEnvPairs(root: string): EnvPair[] {
  let entries: string[] = [];
  try {
    entries = readdirSync(root);
  } catch {
    return [];
  }
  const pairs: EnvPair[] = [];
  const seen = new Set<string>();
  for (const example of entries.filter((n) => EXAMPLE_RE.test(n)).sort()) {
    const target = exampleTarget(example);
    if (seen.has(target)) continue;
    seen.add(target);
    pairs.push({ example, target });
  }
  return pairs;
}

export interface EnvAudit {
  exampleKeys: string[];
  targetKeys: string[];
  /** In the example but not in the target — the keys you still need to set. */
  missing: string[];
  /** In the target but not the example — informational (stale or extra). */
  extra: string[];
  targetExists: boolean;
}

/** Compare the example's key names against the target's (names only — values never read out). */
export function auditEnv(root: string, pair: EnvPair): EnvAudit {
  const exampleKeys = parseEnvKeys(readText(join(root, pair.example)) ?? "");
  const targetText = readText(join(root, pair.target));
  const targetKeys = targetText === undefined ? [] : parseEnvKeys(targetText);
  const targetSet = new Set(targetKeys);
  const exampleSet = new Set(exampleKeys);
  return {
    exampleKeys,
    targetKeys,
    missing: exampleKeys.filter((k) => !targetSet.has(k)),
    extra: targetKeys.filter((k) => !exampleSet.has(k)),
    targetExists: targetText !== undefined,
  };
}
