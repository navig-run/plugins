import type { CanonicalAction } from "../config/constants.js";

/**
 * Map a script NAME onto a canonical action. Exact-name match wins; otherwise an alias
 * pattern. Ambiguity (several dev-like scripts) is resolved by the builder/UI, never by a
 * silent default here — this only proposes a single best canonical label per script.
 */
const ALIASES: Array<[CanonicalAction, RegExp]> = [
  ["dev", /^(dev|develop|serve|start:dev|dev:start)$/],
  ["build", /^(build|compile|bundle)$/],
  ["test", /^(test|tests|vitest|jest|mocha|test:unit)$/],
  ["typecheck", /^(typecheck|type-check|tsc|check:types|types)$/],
  ["lint", /^(lint|eslint|check)$/],
  ["lint:fix", /^(lint:fix|format|fmt|prettier)$/],
  ["preview", /^(preview|serve:prod|start:prod)$/],
  ["start", /^(start|serve:start)$/],
  ["deploy", /^(deploy|release|publish|ship)$/],
  ["migrate", /^(migrate|db:migrate|migration:run|prisma:migrate)$/],
  ["seed", /^(seed|db:seed|seed:dev)$/],
  ["clean", /^(clean|clear|rimraf)$/],
  ["reset", /^(reset|db:reset|nuke)$/],
];

export function resolveCanonical(name: string): CanonicalAction | undefined {
  const lower = name.toLowerCase();
  for (const [canon, re] of ALIASES) {
    if (re.test(lower)) return canon;
  }
  return undefined;
}

/**
 * For each canonical key, the set of candidate script names found. Used to detect ambiguity:
 * if more than one script maps to `dev`, the menu lists them rather than guessing.
 */
export function canonicalCandidates(
  scripts: Record<string, string>,
): Map<CanonicalAction, string[]> {
  const map = new Map<CanonicalAction, string[]>();
  for (const name of Object.keys(scripts)) {
    const canon = resolveCanonical(name);
    if (!canon) continue;
    const arr = map.get(canon) ?? [];
    arr.push(name);
    map.set(canon, arr);
  }
  return map;
}
