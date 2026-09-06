import type { Risk } from "../manifest/schema.js";

/**
 * Risk tier for an action. Base tier comes from the canonical key; a keyword scan of the
 * resolved command can ESCALATE (never de-escalate). Dangerous actions get a typed
 * confirmation in the UI and are never auto-run; `--yes` cannot bypass that.
 */
const BASE: Record<string, Risk> = {
  dev: "safe",
  start: "safe",
  preview: "safe",
  test: "safe",
  typecheck: "safe",
  lint: "safe",
  "lint:fix": "safe",
  build: "confirm",
  migrate: "confirm",
  seed: "confirm",
  deploy: "dangerous",
  clean: "dangerous",
  reset: "dangerous",
};

const DANGER_KEYWORDS =
  /(\bdeploy\b|\bpublish\b|\brelease\b|\breset\b|\bdrop\b|\bprune\b|--force\b|\bdb:reset\b|\bmigrate\s+reset\b|\bdown\s+-v\b|\brm\s+-rf\b|\bnuke\b)/i;
const CONFIRM_KEYWORDS = /(\bmigrate\b|\bseed\b|\bpush\b|\bgenerate\b|\bbuild\b)/i;

const ORDER: Record<Risk, number> = { safe: 0, confirm: 1, dangerous: 2 };

export function riskFor(name: string, command: string, canonical?: string): Risk {
  let risk: Risk = canonical ? (BASE[canonical] ?? "safe") : "safe";
  const hay = `${name} ${command}`;
  if (DANGER_KEYWORDS.test(hay)) risk = max(risk, "dangerous");
  else if (CONFIRM_KEYWORDS.test(hay)) risk = max(risk, "confirm");
  return risk;
}

function max(a: Risk, b: Risk): Risk {
  return ORDER[b] > ORDER[a] ? b : a;
}
