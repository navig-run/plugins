import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { recentsPath } from "../manifest/paths.js";
import type { Action } from "../manifest/schema.js";

const HISTORY_VERSION = 1;
const DEFAULT_LIMIT = 8;

export interface RecentAction {
  id: string;
  canonical?: string;
  label: string;
  group: string;
  ranAt: string;
  count: number;
}

export interface MenuHistory {
  version: typeof HISTORY_VERSION;
  recent: RecentAction[];
}

export function emptyHistory(): MenuHistory {
  return { version: HISTORY_VERSION, recent: [] };
}

export function readMenuHistory(root: string): MenuHistory {
  try {
    const parsed = JSON.parse(readFileSync(recentsPath(root), "utf8")) as Partial<MenuHistory>;
    if (parsed.version !== HISTORY_VERSION || !Array.isArray(parsed.recent)) return emptyHistory();
    return {
      version: HISTORY_VERSION,
      recent: parsed.recent
        .filter((item): item is RecentAction => isRecentAction(item))
        .sort((a, b) => Date.parse(b.ranAt) - Date.parse(a.ranAt)),
    };
  } catch {
    return emptyHistory();
  }
}

export function resolveRecentActions(root: string, actions: Action[], limit = DEFAULT_LIMIT): Action[] {
  if (limit <= 0) return [];
  const byId = new Map(actions.map((action) => [action.id, action]));
  const byCanonical = new Map(actions.flatMap((action) => (action.canonical ? [[action.canonical, action]] : [])));
  const seen = new Set<string>();
  const out: Action[] = [];

  for (const item of readMenuHistory(root).recent) {
    const action = byId.get(item.id) ?? (item.canonical ? byCanonical.get(item.canonical) : undefined);
    if (!action || seen.has(action.id)) continue;
    seen.add(action.id);
    out.push(action);
    if (out.length >= limit) break;
  }
  return out;
}

export function recordMenuAction(root: string, action: Action, limit = DEFAULT_LIMIT): void {
  if (limit <= 0) return;
  const history = readMenuHistory(root);
  const existing = history.recent.find(
    (item) => item.id === action.id || Boolean(action.canonical && item.canonical === action.canonical),
  );
  const next: RecentAction = {
    id: action.id,
    canonical: action.canonical,
    label: action.label,
    group: action.group,
    ranAt: new Date().toISOString(),
    count: (existing?.count ?? 0) + 1,
  };
  const recent = [next, ...history.recent.filter((item) => item !== existing)].slice(0, limit);
  writeHistory(root, { version: HISTORY_VERSION, recent });
}

/** Forget all recorded recents for a project. */
export function clearRecents(root: string): void {
  writeHistory(root, emptyHistory());
}

function writeHistory(root: string, history: MenuHistory): void {
  const path = recentsPath(root);
  try {
    mkdirSync(dirname(path), { recursive: true });
    const tmp = `${path}.${process.pid}.tmp`;
    writeFileSync(tmp, JSON.stringify(history, null, 2) + "\n", "utf8");
    renameSync(tmp, path);
  } catch {
    /* recents are convenience state; read-only projects still run normally */
  }
}

function isRecentAction(value: unknown): value is RecentAction {
  if (!value || typeof value !== "object") return false;
  const item = value as Record<string, unknown>;
  return (
    typeof item.id === "string" &&
    (item.canonical === undefined || typeof item.canonical === "string") &&
    typeof item.label === "string" &&
    typeof item.group === "string" &&
    typeof item.ranAt === "string" &&
    typeof item.count === "number"
  );
}
