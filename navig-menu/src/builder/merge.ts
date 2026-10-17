import type { Action, MenuDefinition, Risk } from "../manifest/schema.js";
import { groupFor } from "./classify.js";

type OverrideMeta = {
  cmd?: string;
  label?: string;
  description?: string;
  risk?: Risk;
  longRunning?: boolean;
  group?: string;
};

/**
 * Apply the human/AI-owned definition (.navig/menu.json) over detected actions. The override
 * ALWAYS wins, but we never drop detected actions silently — hidden ones are removed, canonical
 * overrides replace the command, extras are appended. This is how a re-build preserves edits.
 */
export function applyDefinition(actions: Action[], def: MenuDefinition | undefined): Action[] {
  if (!def) return actions;
  const hide = new Set(def.hide ?? []);
  let result = actions.filter((a) => !hide.has(a.id) && !(a.canonical && hide.has(a.canonical)));

  // Canonical command overrides: replace the launcher/argv of the matching canonical action.
  for (const [canon, spec] of Object.entries(def.actions ?? {})) {
    if (hide.has(canon)) continue; // a hidden canonical must not be re-introduced by its override
    const cmd = typeof spec === "string" ? spec : spec.cmd;
    const tokens = tokenize(cmd);
    if (!tokens.length) continue;
    const launcher = tokens[0]!;
    const argv = tokens.slice(1);
    const meta: OverrideMeta = typeof spec === "string" ? {} : spec;
    const existing = result.find((a) => a.canonical === canon);
    if (existing) {
      existing.launcher = launcher;
      existing.argv = argv;
      existing.confidence = "explicit";
      existing.why = `override: .navig/menu.json actions.${canon}`;
      if (meta.label) existing.label = meta.label;
      if (meta.description) existing.description = meta.description;
      if (meta.risk) existing.risk = meta.risk;
      if (meta.longRunning !== undefined) existing.longRunning = meta.longRunning;
      if (meta.group) existing.group = meta.group;
    } else {
      result.push({
        id: canon,
        canonical: canon,
        label: meta.label ?? humanize(canon),
        description: meta.description,
        group: meta.group ?? groupFor(canon, canon as never),
        launcher,
        argv,
        cwd: ".",
        risk: meta.risk ?? "safe",
        longRunning: meta.longRunning ?? false,
        confidence: "explicit",
        why: `override: .navig/menu.json actions.${canon}`,
        evidence: [],
        source: "custom",
      });
    }
  }

  // Extra custom items.
  for (const e of def.extra ?? []) {
    if (hide.has(e.id)) continue;
    const tokens = tokenize(e.cmd);
    if (!tokens.length) continue;
    result.push({
      id: e.id,
      label: e.label,
      description: e.description,
      group: e.group ?? "Misc",
      launcher: tokens[0]!,
      argv: tokens.slice(1),
      cwd: e.cwd ?? ".",
      risk: e.risk ?? "safe",
      longRunning: e.longRunning ?? false,
      confidence: "explicit",
      why: e.why ?? "override: .navig/menu.json extra",
      note: e.note,
      evidence: [],
      source: "custom",
    });
  }

  // Per-script overrides: relabel / regroup / redescribe / re-risk detected scripts in place.
  const overrides = def.overrides ?? {};
  result = result.filter((a) => !overrides[a.id]?.hide);
  for (const a of result) {
    const o = overrides[a.id];
    if (!o) continue;
    if (o.label) a.label = o.label;
    if (o.description !== undefined) a.description = o.description;
    if (o.group) a.group = o.group;
    if (o.risk) a.risk = o.risk;
    if (o.longRunning !== undefined) a.longRunning = o.longRunning;
    if (o.note !== undefined) a.note = o.note;
  }

  return result;
}

/**
 * Minimal, shell-free tokenizer. Splits on whitespace, respecting single/double quotes. We
 * still execute argv as an ARRAY (no shell), so this only structures the command — it never
 * enables shell interpolation.
 */
export function tokenize(cmd: string): string[] {
  const out: string[] = [];
  const re = /"([^"]*)"|'([^']*)'|(\S+)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(cmd))) out.push(m[1] ?? m[2] ?? m[3] ?? "");
  return out.filter((t) => t.length > 0);
}

function humanize(name: string): string {
  return name
    .split(/[:_-]/)
    .map((p) => (p ? p[0]!.toUpperCase() + p.slice(1) : p))
    .join(" ");
}
