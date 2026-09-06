import type { Action, Risk } from "../manifest/schema.js";
import type { MenuModel } from "../builder/build.js";
import { tokenize } from "../builder/merge.js";
import { GROUPS, type Group } from "../builder/classify.js";

/**
 * Data-driven DSL for defining custom menus without touching terminal internals. Build a model
 * with `createMenu().section(...)` and hand it to `runMenu`. This is the ergonomic surface an AI
 * (or a human) uses to author menus — see docs/dsl.md for the <30-line example.
 */
export interface ActionInit {
  label: string;
  cmd?: string;
  description?: string;
  risk?: Risk;
  longRunning?: boolean;
}

export interface ActionDef extends ActionInit {
  id: string;
}

export function action(id: string, init: ActionInit): ActionDef {
  return { id, ...init };
}

export class MenuBuilder {
  private items: Action[] = [];
  constructor(
    private readonly meta: { title: string; purpose?: string; accent?: string },
  ) {}

  section(group: Group | string, defs: ActionDef[]): this {
    for (const d of defs) {
      const tokens = d.cmd ? tokenize(d.cmd) : [];
      this.items.push({
        id: d.id,
        label: d.label,
        description: d.description,
        group: String(group),
        launcher: tokens[0] ?? "",
        argv: tokens.slice(1),
        cwd: ".",
        risk: d.risk ?? "safe",
        longRunning: d.longRunning ?? false,
        confidence: "explicit",
        evidence: [],
        source: "custom",
      });
    }
    return this;
  }

  toModel(): MenuModel {
    const groups = GROUPS.map((group) => ({
      group,
      items: this.items.filter((a) => a.group === group),
    })).filter((g) => g.items.length > 0);
    // Any custom group names not in GROUPS get appended in declaration order.
    const extraGroups = [...new Set(this.items.map((a) => a.group))].filter(
      (g) => !GROUPS.includes(g as Group),
    );
    for (const g of extraGroups) {
      groups.push({ group: g as Group, items: this.items.filter((a) => a.group === g) });
    }
    return {
      root: process.cwd(),
      title: this.meta.title,
      purpose: this.meta.purpose,
      accent: this.meta.accent,
      packageManager: "none",
      pmConfidence: "unknown",
      frameworks: [],
      services: [],
      endpoints: [],
      packages: [],
      workspaceKind: "none",
      stats: { scripts: this.items.length, packages: 0, services: 0, docs: 0 },
      groups,
      allScripts: this.items,
      warnings: [],
    };
  }
}

export function createMenu(meta: { title: string; purpose?: string; accent?: string }): MenuBuilder {
  return new MenuBuilder(meta);
}
