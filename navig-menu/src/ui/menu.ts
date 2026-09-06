import { emitKeypressEvents } from "node:readline";
import { PassThrough, type Readable } from "node:stream";
import type { Action, Manifest } from "../manifest/schema.js";
import type { MenuModel } from "../builder/build.js";
import { createTheme, ACCENT_NAMES, type Theme } from "./theme.js";
import type { GitInfo } from "../util/git.js";
import { input, confirmPrompt, CANCEL } from "./prompts.js";
import { reviewBlock, confirmRisk, type ConfirmOptions } from "./confirm.js";
import { runActionLocal, type RunResult } from "../runners/local.js";
import { diagnose } from "../runners/diagnose.js";
import { aiAvailable, aiDiagnose, buildDiagnosisPrompt, aiPickAction } from "../runners/ai.js";
import { emitActionRequest } from "../runners/relay.js";
import { recordMenuAction, resolveRecentActions, clearRecents } from "./history.js";
import { renderCosmicBanner } from "./cosmic.js";
import { sectionMeta, GROUPS, type ToneKey } from "../builder/classify.js";
import { resolveSettings, type Settings, type GlyphSetting } from "./settings.js";
import type { GlyphStyle } from "../manifest/schema.js";
import { definitionPath } from "../manifest/paths.js";
import { makeActionContext, runPluginHandler } from "../plugins/apply.js";
import type { LoadedPlugin, PluginSummary, ResolvedPluginSetting } from "../plugins/types.js";
import { PROJECT_URL, AUTHOR_GITHUB, LICENSE } from "../config/constants.js";

export interface MenuRuntime {
  root: string;
  theme: Theme;
  settings: Settings;
  plain: boolean;
  mode: string;
  relay: boolean;
  opts: ConfirmOptions;
  git: GitInfo;
  manifest: Manifest;
  toolVersion: string;
  /** All detected actions (pre-hide) — what the customizer lists. */
  allActions: Action[];
  /** Currently-hidden action ids (from .navig/menu.json `hide`). */
  hidden: string[];
  /** Re-scan (no cache) + rebuild the model — used by "Refresh scan". */
  rebuild: () => MenuModel;
  /** Write .navig/menu.json from detection + reload — "Regenerate menu". */
  regenerate: () => MenuModel;
  /** AI: describe every un-explained command + reload — "Organize with AI". Null if unavailable. */
  organize?: () => Promise<MenuModel | null>;
  /** Persist settings to .navig/menu.json + reload the model with them applied. */
  persistSettings: (next: Settings) => MenuModel;
  /** Persist the hidden-items list + reload the model. */
  persistHidden: (ids: string[]) => MenuModel;
  /** Loaded plugins (for internal-action handler dispatch). */
  loaded: LoadedPlugin[];
  /** Persist a plugin's activation / settings + reload the model. */
  persistPluginState: (id: string, patch: { enabled?: boolean; settings?: Record<string, boolean | string> }) => MenuModel;
  /** Persist a personal pre-run note for an action (blank clears it) + reload the model. */
  persistNote: (id: string, note: string) => MenuModel;
}

export interface MenuRenderRuntime {
  root: string;
  theme: Theme;
  mode: string;
  git: GitInfo;
  manifest: Manifest;
  settings?: Settings;
  toolVersion?: string;
}

export interface MenuSnapshotOptions {
  width?: number;
  height?: number;
  filter?: string;
  settings?: Settings;
}

type View = "menu" | "details" | "help" | "settings" | "customizer" | "plugins" | "about";

interface NavigItem {
  id: "settings" | "customize" | "plugins" | "regenerate" | "refresh" | "doctor" | "edit" | "about" | "quit";
  label: string;
  description: string;
  glyph: string;
}

/** Connector geometry for the nested tree layout, precomputed so rendering stays theme-only. */
interface TreeMeta {
  /** 0 = a top-level section under the pinned root; deeper = nested namespace folders. */
  depth: number;
  /** For each ancestor level (root-side → parent): was that ancestor its siblings' last child? */
  ancestorLast: boolean[];
  /** Is this node the last among its own siblings? (picks └─ vs ├─) */
  isLast: boolean;
}

type Row =
  | { kind: "section"; label: string; count?: number; tree?: TreeMeta }
  | { kind: "action"; action: Action; recent?: boolean; tree?: TreeMeta }
  | { kind: "navig"; item: NavigItem; tree?: TreeMeta }
  | { kind: "category"; key: string; count: number; preview: string }
  | { kind: "folder"; label: string; count?: number; tree: TreeMeta }
  | { kind: "empty"; label: string };

interface Keypress {
  str: string;
  name?: string;
  sequence?: string;
  ctrl?: boolean;
}

interface MenuState {
  selected: number;
  offset: number;
  filter: string;
  message?: string;
  view: View;
  settingsIndex: number;
  settingsOffset: number;
  customizerIndex: number;
  customizerOffset: number;
  pluginIndex: number;
  pluginOffset: number;
  /** In "categories" layout: the entered category key, or null at the picker level. */
  category: string | null;
  /** Selection index to restore when leaving a category. */
  pickerIndex: number;
}

const MIN_WIDTH = 56;
const MAX_WIDTH = 88;

// The rail is deliberately tiny — everything else lives inside Settings (opened from here).
const NAVIG_ITEMS: NavigItem[] = [
  { id: "settings", label: "Settings", description: "customize · plugins · regenerate · refresh · doctor · edit", glyph: "⚙" },
  { id: "about", label: "About", description: "studio, website, author, loaded plugins", glyph: "★" },
  { id: "quit", label: "Quit", description: "leave the menu", glyph: "⏻" },
];

const NAVIG_ASCII: Record<NavigItem["id"], string> = {
  settings: "*",
  customize: "#",
  plugins: "&",
  regenerate: "@",
  refresh: "~",
  doctor: "?",
  edit: "/",
  about: "i",
  quit: "x",
};

/**
 * Premium keyboard TUI. A cosmic banner over a grouped, mathematically-aligned command palette with
 * recents, type-to-search, live settings, a menu customizer (hide/show items), and a UTILITIES rail
 * (settings / customize / regenerate / refresh / doctor). Selection is clamped against a fixed
 * viewport, so navigation is deterministic.
 */
export async function runMenu(initial: MenuModel, rt: MenuRuntime): Promise<number> {
  let model = initial;
  let theme = rt.theme;
  let settings = rt.settings;
  let draft: Settings = settings;
  let hiddenSet = new Set(rt.hidden);
  let hiddenDraft = new Set(hiddenSet);
  let pluginDraft = new Map<string, boolean | string>();
  const state: MenuState = {
    selected: 0,
    offset: 0,
    filter: "",
    view: "menu",
    settingsIndex: 0,
    settingsOffset: 0,
    customizerIndex: 0,
    customizerOffset: 0,
    pluginIndex: 0,
    pluginOffset: 0,
    category: null,
    pickerIndex: 0,
  };

  if (!process.stdin.isTTY || !process.stdout.isTTY) return 0;

  const rawBefore = process.stdin.isRaw;
  // Input pipeline. With mouse ON, keys flow through a PassThrough fed by a raw-data filter that
  // strips mouse escape sequences (readline mangles SGR mouse into stray digit keys — the "6666"
  // bug) and surfaces wheel events out-of-band. With mouse OFF, keypresses come straight off stdin
  // exactly as before.
  const wheelQueue: Keypress[] = [];
  let wheelWaiter: ((k: Keypress) => void) | null = null;
  const emitWheel = (name: "wheelup" | "wheeldown"): void => {
    const k: Keypress = { str: "", name };
    if (wheelWaiter) { const w = wheelWaiter; wheelWaiter = null; w(k); }
    else wheelQueue.push(k);
  };
  const keyStream = settings.mouse ? new PassThrough() : undefined;
  const keySource: Readable = keyStream ?? process.stdin;
  emitKeypressEvents(keySource);
  const readKey = (): Promise<Keypress> =>
    new Promise((resolve) => {
      if (wheelQueue.length) { resolve(wheelQueue.shift()!); return; }
      const cleanup = (): void => { keySource.off("keypress", onKey); wheelWaiter = null; };
      const onKey = (str: string, key: Omit<Keypress, "str"> = {}): void => { cleanup(); resolve({ str, ...key }); };
      wheelWaiter = (k) => { cleanup(); resolve(k); };
      keySource.on("keypress", onKey);
    });
  const terminal = new TerminalFrame(rawBefore, keyStream, emitWheel);
  terminal.enter();

  const viewRt = (): MenuRenderRuntime => ({
    root: rt.root,
    theme,
    mode: rt.mode,
    git: rt.git,
    manifest: rt.manifest,
    settings,
    toolVersion: rt.toolVersion,
  });

  const runPluginAction = async (action: Action): Promise<void> => {
    const [, , pluginId = "", handlerKey = ""] = action.source.split(":");
    terminal.leave();
    console.clear();
    const { c } = theme;
    console.log("\n  " + c.dim(action.description ?? action.label) + "\n");
    let msg = "";
    const settingsFor: Record<string, boolean | string> = {};
    for (const s of model.pluginSettings ?? []) if (s.pluginId === pluginId) settingsFor[s.key] = s.value;
    const actx = makeActionContext({
      root: rt.root,
      manifest: rt.manifest,
      pluginId,
      settings: settingsFor,
      theme,
      notify: (m) => {
        msg = m;
      },
    });
    await runPluginHandler(rt.loaded, pluginId, handlerKey, actx);
    if (msg) console.log("  " + c.green(msg));
    await pause(theme);
    terminal.enter();
    state.message = msg || "done";
  };

  const runItem = async (action: Action): Promise<boolean> => {
    if (action.source.startsWith("plugin:internal:")) {
      await runPluginAction(action);
      return false;
    }
    terminal.leave();
    console.clear();
    console.log(reviewBlock(action, rt.root, theme));
    // A personal note on an otherwise-safe command still gets an explicit acknowledgement, so the
    // info (logins, reminders) is actually read before the command runs.
    if (action.note && action.risk === "safe") {
      const go = await confirmPrompt(`  Run ${theme.c.white(action.label)}?`, true);
      if (go !== true) {
        await pause(theme, "Cancelled.");
        terminal.enter();
        state.message = "cancelled";
        return false;
      }
    }
    const ok = await confirmRisk(action, theme, rt.opts, rt.root);
    if (!ok) {
      await pause(theme, "Cancelled.");
      terminal.enter();
      state.message = "cancelled";
      return false;
    }

    recordMenuAction(rt.root, action, settings.recentLimit);
    if (rt.relay) {
      emitActionRequest(action, rt.root);
      return true;
    }

    console.log("");
    const res = await runActionLocal(action, rt.root);
    const { c, sym } = theme;
    if (res.cancelled) console.log("\n  " + c.yellow(`${sym.warning} cancelled (exit 130)`));
    else if (res.exitCode === 0) console.log("\n  " + c.green(`${sym.complete} done`));
    else {
      console.log("\n  " + c.red(`${sym.failed} exit ${res.exitCode}`));
      await offerDiagnosis(action, res, rt.root, theme);
    }
    await pause(theme);
    terminal.enter();
    state.message = res.exitCode === 0 ? "done" : `exit ${res.exitCode}`;
    return false;
  };

  // Attach / edit a personal pre-run note on the selected command (persisted to .navig/menu.json).
  const editNote = async (rows: Row[], st: MenuState): Promise<void> => {
    const action = selectedAction(rows, st.selected);
    if (!action || action.source.startsWith("plugin:")) {
      if (action) st.message = "notes aren't available for plugin actions";
      return;
    }
    terminal.leave();
    console.clear();
    const { c } = theme;
    console.log(
      "\n  " + c.dim("Pre-run note for ") + c.white(action.label) +
      c.dim(" — shown before every run. Blank clears it."),
    );
    if (action.note) console.log("  " + c.dim("current: ") + c.yellowBright(action.note.replace(/\n/g, " · ")));
    console.log("");
    const note = await input("  note");
    terminal.enter();
    if (note === CANCEL) {
      st.message = "cancelled";
      return;
    }
    model = rt.persistNote(action.id, note);
    st.message = note.trim() ? "note saved" : "note cleared";
  };

  // Natural-language fallback: when a search matches nothing, let AI map the query to an existing
  // action (map-only — it can only pick a real action, never synthesize a command).
  const nlResolve = async (query: string): Promise<Action | null> => {
    const items = model.groups.flatMap((g) => g.items);
    if (!items.length) return null;
    terminal.leave();
    console.log("\n  " + theme.c.dim(`Finding an action for "${query}"…`));
    const id = await aiPickAction(
      query,
      items.map((a) => ({ id: a.id, label: a.label, cmd: `${a.launcher} ${a.argv.join(" ")}`.trim() })),
    );
    const picked = id ? items.find((a) => a.id === id) ?? null : null;
    if (!picked) {
      console.log("  " + theme.c.dim("No matching action — try different words, or esc to clear."));
      await pause(theme);
      terminal.enter();
    }
    return picked; // when found, leave the terminal for runItem to manage
  };

  const reTheme = (s: Settings) => createTheme({ plain: rt.plain, accent: s.accent, glyphs: glyphsOf(s) });

  const openSettings = () => {
    draft = { ...settings, banner: { ...settings.banner } };
    state.view = "settings";
    state.settingsIndex = firstSelectableSetting();
    state.settingsOffset = 0;
  };

  const openCustomizer = () => {
    hiddenDraft = new Set(hiddenSet);
    state.view = "customizer";
    state.customizerIndex = 0;
    state.customizerOffset = 0;
  };

  const saveSettings = () => {
    settings = { ...draft, banner: { ...draft.banner } };
    model = rt.persistSettings(settings);
    theme = reTheme(settings);
    state.view = "menu";
    state.category = null; // layout may have changed
    state.selected = 0;
    state.offset = 0;
    state.message = "settings saved";
  };

  const saveCustomizer = () => {
    hiddenSet = new Set(hiddenDraft);
    model = rt.persistHidden([...hiddenSet]);
    settings = resolveSettings(model.ui, model.accent);
    theme = reTheme(settings);
    state.view = "menu";
    state.category = null;
    state.selected = 0;
    state.offset = 0;
    state.message = `customizer saved · ${hiddenSet.size} hidden`;
  };

  const openPluginManager = () => {
    pluginDraft = new Map();
    for (const p of model.loadedPlugins ?? []) pluginDraft.set(p.id, p.active);
    for (const s of model.pluginSettings ?? []) pluginDraft.set(`${s.pluginId}::${s.key}`, s.value);
    state.view = "plugins";
    state.pluginIndex = 0;
    state.pluginOffset = 0;
  };

  const savePluginManager = () => {
    for (const p of model.loadedPlugins ?? []) {
      const enabled = pluginDraft.get(p.id);
      const settingsPatch: Record<string, boolean | string> = {};
      for (const s of model.pluginSettings ?? []) {
        if (s.pluginId !== p.id) continue;
        const v = pluginDraft.get(`${p.id}::${s.key}`);
        if (v !== undefined) settingsPatch[s.key] = v;
      }
      model = rt.persistPluginState(p.id, {
        enabled: typeof enabled === "boolean" ? enabled : undefined,
        settings: Object.keys(settingsPatch).length ? settingsPatch : undefined,
      });
    }
    settings = resolveSettings(model.ui, model.accent);
    theme = reTheme(settings);
    state.view = "menu";
    state.category = null;
    state.selected = 0;
    state.offset = 0;
    state.message = "plugins updated";
  };

  const dispatchNavig = (item: NavigItem): number | undefined => {
    switch (item.id) {
      case "settings":
        openSettings();
        return undefined;
      case "customize":
        openCustomizer();
        return undefined;
      case "plugins":
        openPluginManager();
        return undefined;
      case "about":
        state.view = "about";
        return undefined;
      case "refresh":
        model = rt.rebuild();
        resetNav(state);
        state.message = "scan refreshed";
        return undefined;
      case "regenerate":
        model = rt.regenerate();
        settings = resolveSettings(model.ui, model.accent);
        theme = reTheme(settings);
        resetNav(state);
        state.message = "menu regenerated → .navig/menu.json";
        return undefined;
      case "doctor":
        state.view = "details";
        return undefined;
      case "edit":
        state.message = `edit: ${definitionPath(rt.root)}`;
        return undefined;
      case "quit":
        return 0;
    }
  };

  try {
    while (true) {
      const rows = rowsFor(model, rt.root, state.filter, settings, state.category);
      normalizeSelection(state, rows);
      renderFrame(model, viewRt(), state, rows, draft, rt.allActions, hiddenDraft, pluginDraft);
      const key = await readKey();
      state.message = undefined;

      if (key.ctrl && key.name === "c") return 130;

      // Mouse wheel → list scroll. A quick multi-row jump in the main menu; a single up/down
      // elsewhere so each sub-view's own handler moves by one.
      if (key.name === "wheelup" || key.name === "wheeldown") {
        const dir = key.name === "wheeldown" ? 1 : -1;
        if (state.view === "menu") {
          moveSelection(state, rows, dir * 3);
          continue;
        }
        key.name = dir < 0 ? "up" : "down";
      }

      if (state.view === "settings") {
        const sig = handleSettingsKey(key, state, draft);
        if (typeof sig === "string" && sig.startsWith("open:")) {
          saveSettings(); // persist appearance changes before jumping to a sub-view
          const v = sig.slice(5);
          if (v === "customizer") openCustomizer();
          else if (v === "plugins") openPluginManager();
          else if (v === "details") state.view = "details";
        } else if (typeof sig === "string" && sig.startsWith("action:")) {
          saveSettings();
          const a = sig.slice(7);
          if (a === "regenerate") {
            model = rt.regenerate();
            settings = resolveSettings(model.ui, model.accent);
            theme = reTheme(settings);
            resetNav(state);
            state.message = "menu regenerated → .navig/menu.json";
          } else if (a === "organize") {
            if (!rt.organize) {
              state.message = "organize unavailable";
            } else {
              const next = await rt.organize();
              if (next) {
                model = next;
                settings = resolveSettings(model.ui, model.accent);
                theme = reTheme(settings);
                resetNav(state);
                state.message = "menu organized → .navig/menu.json";
              } else {
                state.message = "organize needs an AI key (or nothing to describe)";
              }
            }
          } else if (a === "refresh") {
            model = rt.rebuild();
            resetNav(state);
            state.message = "scan refreshed";
          } else if (a === "clearRecents") {
            clearRecents(rt.root);
            state.message = "recents cleared";
          } else if (a === "edit") {
            state.message = `edit: ${definitionPath(rt.root)}`;
          }
        } else if (sig === "changed") {
          theme = reTheme(draft); // live preview
        } else if (isQuit(key) || key.name === "escape" || key.name === "return" || key.name === "s") {
          saveSettings();
        }
        continue;
      }

      if (state.view === "customizer") {
        if (handleCustomizerKey(key, state, rt.allActions, hiddenDraft)) continue;
        if (isQuit(key) || key.name === "escape" || key.name === "return" || key.name === "s") {
          saveCustomizer();
        }
        continue;
      }

      if (state.view === "plugins") {
        if (handlePluginKey(key, state, model, pluginDraft)) continue;
        if (isQuit(key) || key.name === "escape" || key.name === "return" || key.name === "s") {
          savePluginManager();
        }
        continue;
      }

      if (state.view !== "menu") {
        if (isQuit(key) || key.name === "escape" || key.name === "backspace" || key.name === "return") {
          state.view = "menu";
        }
        continue;
      }

      if (state.filter) {
        if (key.name === "escape") {
          state.filter = "";
          state.selected = 0;
          state.offset = 0;
          continue;
        }
        if (key.name === "backspace") {
          state.filter = state.filter.slice(0, -1);
          state.selected = 0;
          continue;
        }
        if (key.name === "return") {
          const action = selectedAction(rows, state.selected);
          if (action) {
            if (await runItem(action)) return 0;
            continue;
          }
          // No literal match — offer AI natural-language mapping.
          if (aiAvailable() && state.filter.trim()) {
            const picked = await nlResolve(state.filter);
            if (picked) {
              state.filter = "";
              if (await runItem(picked)) return 0;
            }
          }
          continue;
        }
        if (key.name === "up") moveSelection(state, rows, -1);
        else if (key.name === "down") moveSelection(state, rows, 1);
        else appendFilter(state, key);
        continue;
      }

      if (key.name === "up") moveSelection(state, rows, -1);
      else if (key.name === "down") moveSelection(state, rows, 1);
      else if (key.name === "pageup") moveSelection(state, rows, -pageSize());
      else if (key.name === "pagedown") moveSelection(state, rows, pageSize());
      else if (key.name === "home") moveToEdge(state, rows, "first");
      else if (key.name === "end") moveToEdge(state, rows, "last");
      else if (key.name === "return") {
        const row = rows[state.selected];
        if (row?.kind === "category") {
          state.pickerIndex = state.selected;
          state.category = row.key;
          state.selected = 0;
          state.offset = 0;
        } else if (row?.kind === "navig") {
          const code = dispatchNavig(row.item);
          if (code !== undefined) return code;
        } else {
          const action = selectedAction(rows, state.selected);
          if (action && (await runItem(action))) return 0;
        }
      } else if ((key.name === "left" || key.name === "backspace") && state.category) {
        exitCategory(state);
      } else if (key.name === "slash" || key.str === "/") {
        state.filter = "";
        state.message = "search";
      } else if (key.name === "r") {
        model = rt.rebuild();
        resetNav(state);
        state.message = "scan refreshed";
      } else if (key.name === "g") {
        model = rt.regenerate();
        settings = resolveSettings(model.ui, model.accent);
        theme = reTheme(settings);
        resetNav(state);
        state.message = "menu regenerated → .navig/menu.json";
      } else if (key.name === "s" || key.str === ",") {
        openSettings();
      } else if (key.name === "c") {
        openCustomizer();
      } else if (key.name === "p") {
        openPluginManager();
      } else if (key.name === "a") {
        state.view = "about";
      } else if (key.name === "d") {
        state.view = "details";
      } else if (key.name === "n") {
        await editNote(rows, state);
      } else if (key.name === "h" || key.str === "?") {
        state.view = "help";
      } else if (key.name === "escape") {
        if (state.category) exitCategory(state);
        else return 0;
      } else if (isQuit(key)) {
        return 0;
      } else {
        appendFilter(state, key);
      }
    }
  } finally {
    terminal.leave();
  }
}

interface MenuCategory {
  key: string;
  count: number;
  rows: Row[];
}

/** Re-group every command by its owning project (for `layout: "projects"`); workspace-wide last. */
function groupsByProject(model: MenuModel): { group: string; items: Action[] }[] {
  const byProject = new Map<string, Action[]>();
  for (const a of model.allScripts) {
    const key = a.project ?? "workspace";
    const bucket = byProject.get(key) ?? [];
    if (!byProject.has(key)) byProject.set(key, bucket);
    bucket.push(a);
  }
  const keys = [...byProject.keys()].sort((a, b) =>
    a === "workspace" ? 1 : b === "workspace" ? -1 : a.localeCompare(b),
  );
  return keys.map((k) => ({ group: k.toUpperCase(), items: byProject.get(k)! }));
}

/** The categories, in display order, each with the rows to reveal once entered. */
function menuCategories(model: MenuModel, root: string, settings: Settings): MenuCategory[] {
  const cats: MenuCategory[] = [];
  if (settings.showRecents && settings.recentLimit > 0) {
    const recents = resolveRecentActions(root, model.allScripts, settings.recentLimit);
    if (recents.length) {
      cats.push({ key: "Recent", count: recents.length, rows: recents.map((action) => ({ kind: "action", action, recent: true })) });
    }
  }
  for (const group of model.groups) {
    cats.push({ key: group.group, count: group.items.length, rows: group.items.map((action) => ({ kind: "action", action })) });
  }
  if (settings.showNavigSection) {
    cats.push({ key: "Utilities", count: NAVIG_ITEMS.length, rows: NAVIG_ITEMS.map((item) => ({ kind: "navig", item })) });
  }
  return cats;
}

function rowLabel(row: Row): string {
  if (row.kind === "action") return row.action.label;
  if (row.kind === "navig") return row.item.label;
  return "";
}

function rowsFor(model: MenuModel, root: string, filter: string, settings: Settings, category?: string | null): Row[] {
  const q = filter.trim().toLowerCase();
  if (q) {
    const matches = model.allScripts
      .map((action) => ({ action, score: matchScore(action, q) }))
      .filter((item) => item.score > 0)
      .sort((a, b) => b.score - a.score || a.action.label.localeCompare(b.action.label))
      .map<Row>((item) => ({ kind: "action", action: item.action }));
    return [
      { kind: "section", label: `Search "${filter}"`, count: matches.length },
      ...(matches.length ? matches : ([{ kind: "empty", label: "No matching commands" }] satisfies Row[])),
    ];
  }

  // Categories layout: a drill-down picker → enter a category → its items → back.
  if (settings.layout === "categories") {
    const cats = menuCategories(model, root, settings);
    if (!cats.length) return [{ kind: "empty", label: "No scripts detected yet" }];
    const entered = category ? cats.find((c) => c.key === category) : undefined;
    if (!entered) {
      return cats.map<Row>((c) => ({
        kind: "category",
        key: c.key,
        count: c.count,
        preview: previewOf(c.rows),
      }));
    }
    return [{ kind: "section", label: entered.key, count: entered.count }, ...entered.rows];
  }

  // Nested tree: sections → namespace sub-folders → commands, all expanded on one page.
  if (settings.layout === "tree") return buildTreeRows(model, root, settings);

  // List layout: one flat, unsectioned list — every command in a single stream (no rules, no tree).
  if (settings.layout === "list") {
    const rows: Row[] = [];
    for (const group of model.groups) rows.push(...group.items.map<Row>((action) => ({ kind: "action", action })));
    if (!model.allScripts.length) rows.push({ kind: "empty", label: "No scripts detected yet" });
    if (settings.showNavigSection) rows.push(...NAVIG_ITEMS.map<Row>((item) => ({ kind: "navig", item })));
    return rows;
  }

  // Flat layout: grouped sections, everything on one page. "projects" is the same shape but grouped
  // by owning app instead of by function.
  const rows: Row[] = [];
  if (settings.showRecents && settings.recentLimit > 0) {
    const recents = resolveRecentActions(root, model.allScripts, settings.recentLimit);
    if (recents.length) {
      rows.push({ kind: "section", label: "Recent", count: recents.length });
      rows.push(...recents.map<Row>((action) => ({ kind: "action", action, recent: true })));
    }
  }

  const grouped = settings.layout === "projects" ? groupsByProject(model) : model.groups;
  for (const group of grouped) {
    rows.push({ kind: "section", label: group.group, count: group.items.length });
    rows.push(...group.items.map<Row>((action) => ({ kind: "action", action })));
  }

  if (!model.allScripts.length) rows.push({ kind: "empty", label: "No scripts detected yet" });

  if (settings.showNavigSection) {
    rows.push({ kind: "section", label: "Utilities", count: NAVIG_ITEMS.length });
    rows.push(...NAVIG_ITEMS.map<Row>((item) => ({ kind: "navig", item })));
  }
  return rows;
}

/* ── Tree layout: build a nested filesystem from script namespaces ───────────────── */

interface TrieNode {
  name: string;
  action?: Action;
  children: Map<string, TrieNode>;
}

/** Split a script id into path segments — `api:db:migrate` / `api/db/migrate` → api → db → migrate. */
function trieFrom(items: Action[]): TrieNode {
  const root: TrieNode = { name: "", children: new Map() };
  for (const action of items) {
    const segs = action.id.split(/[:/]/).filter(Boolean);
    let node = root;
    for (const seg of segs) {
      let child = node.children.get(seg);
      if (!child) {
        child = { name: seg, children: new Map() };
        node.children.set(seg, child);
      }
      node = child;
    }
    node.action = action;
  }
  return root;
}

function leafCount(node: TrieNode): number {
  let n = node.action ? 1 : 0;
  for (const child of node.children.values()) n += leafCount(child);
  return n;
}

/** DFS a namespace node into flat rows carrying tree geometry (depth + ancestor-last flags). */
function emitTreeNode(node: TrieNode, depth: number, ancestorLast: boolean[], isLast: boolean, out: Row[]): void {
  const tree: TreeMeta = { depth, ancestorLast, isLast };
  if (node.action && node.children.size === 0) {
    out.push({ kind: "action", action: node.action, tree });
  } else if (node.action) {
    // A script that is ALSO a namespace parent (e.g. `store` alongside `store:publish`).
    out.push({ kind: "action", action: node.action, tree });
  } else {
    out.push({ kind: "folder", label: node.name, count: leafCount(node), tree });
  }
  const kids = [...node.children.values()];
  kids.forEach((child, i) => emitTreeNode(child, depth + 1, [...ancestorLast, isLast], i === kids.length - 1, out));
}

function buildTreeRows(model: MenuModel, root: string, settings: Settings): Row[] {
  const out: Row[] = [];
  const sections: { label: string; items: Action[] }[] = [];
  if (settings.showRecents && settings.recentLimit > 0) {
    const recents = resolveRecentActions(root, model.allScripts, settings.recentLimit);
    if (recents.length) sections.push({ label: "Recent", items: recents });
  }
  for (const group of model.groups) sections.push({ label: group.group, items: group.items });

  const hasNavig = settings.showNavigSection;
  const total = sections.length + (hasNavig ? 1 : 0);

  sections.forEach((sec, si) => {
    const isLastSection = !hasNavig && si === total - 1;
    out.push({ kind: "section", label: sec.label, count: sec.items.length, tree: { depth: 0, ancestorLast: [], isLast: isLastSection } });
    const trie = trieFrom(sec.items);
    const kids = [...trie.children.values()];
    kids.forEach((child, i) => emitTreeNode(child, 1, [isLastSection], i === kids.length - 1, out));
  });

  if (hasNavig) {
    out.push({ kind: "section", label: "Utilities", count: NAVIG_ITEMS.length, tree: { depth: 0, ancestorLast: [], isLast: true } });
    NAVIG_ITEMS.forEach((item, i) =>
      out.push({ kind: "navig", item, tree: { depth: 1, ancestorLast: [true], isLast: i === NAVIG_ITEMS.length - 1 } }),
    );
  }

  if (!out.length) out.push({ kind: "empty", label: "No scripts detected yet" });
  return out;
}

function previewOf(rows: Row[]): string {
  const labels = rows.map(rowLabel).filter(Boolean);
  const shown = labels.slice(0, 3).join(" · ");
  return labels.length > 3 ? `${shown} · …` : shown;
}

function renderFrame(
  model: MenuModel,
  rt: MenuRenderRuntime,
  state: MenuState,
  rows: Row[],
  draft: Settings,
  allActions: Action[],
  hiddenDraft: Set<string>,
  pluginDraft: Map<string, boolean | string>,
): void {
  const width = uiWidth();
  const height = Math.max(18, process.stdout.rows ?? 34);
  process.stdout.write(
    "\x1b[H\x1b[2J" +
      renderMenuSnapshot(model, rt, { width, height, filter: state.filter }, state, rows, draft, allActions, hiddenDraft, pluginDraft),
  );
}

export function renderMenuSnapshot(
  model: MenuModel,
  rt: MenuRenderRuntime,
  opts: MenuSnapshotOptions = {},
  existingState?: MenuState,
  existingRows?: Row[],
  draftSettings?: Settings,
  allActions?: Action[],
  hiddenDraft?: Set<string>,
  pluginDraft?: Map<string, boolean | string>,
): string {
  const { theme } = rt;
  const settings = opts.settings ?? rt.settings ?? resolveSettings(model.ui, model.accent);
  const width = opts.width ?? uiWidth();
  const height = Math.max(18, opts.height ?? (process.stdout.rows ?? 34));
  const state = existingState ?? {
    selected: 0,
    offset: 0,
    filter: opts.filter ?? "",
    view: "menu" as View,
    settingsIndex: 0,
    settingsOffset: 0,
    customizerIndex: 0,
    customizerOffset: 0,
    pluginIndex: 0,
    pluginOffset: 0,
    category: null,
    pickerIndex: 0,
  };
  const rows = existingRows ?? rowsFor(model, rt.root, state.filter, settings, state.category);
  normalizeSelection(state, rows);
  // While editing settings, preview the draft everywhere (banner toggles, density, etc.).
  const effective = state.view === "settings" && draftSettings ? draftSettings : settings;
  const header = renderHeader(model, rt, effective, width);
  const footer = renderFooter(model, state, rows, width, theme, effective);
  const bodyHeight = Math.max(1, height - header.length - footer.length);
  const body =
    state.view === "details"
      ? renderDetails(model, rt, width, bodyHeight, theme)
      : state.view === "help"
        ? renderHelp(width, bodyHeight, theme)
        : state.view === "settings"
          ? renderSettings(draftSettings ?? settings, state, width, bodyHeight, theme)
          : state.view === "customizer"
            ? renderCustomizer(allActions ?? rt.manifest.actions, hiddenDraft ?? new Set(), state, width, bodyHeight, theme)
            : state.view === "plugins"
              ? renderPluginManager(model, pluginDraft ?? new Map(), state, width, bodyHeight, theme)
              : state.view === "about"
                ? renderAbout(model, rt, width, bodyHeight, theme)
                : settings.layout === "tree" && !state.filter
                  ? renderTree(rows, state, width, bodyHeight, theme, settings, (model.textOverrides?.title ?? model.title).toUpperCase())
                  : renderRows(rows, state, width, bodyHeight, theme, settings);

  const frame = [...header, ...body, ...footer].slice(0, height);
  return frame.join("\n");
}

function renderHeader(model: MenuModel, rt: MenuRenderRuntime, settings: Settings, width: number): string[] {
  const inner = width - 2;
  const banner = renderCosmicBanner(model, {
    theme: rt.theme,
    git: rt.git,
    mode: rt.mode,
    settings,
    toolVersion: rt.toolVersion ?? rt.manifest.tool.version,
  }, inner);
  const { c, accent } = rt.theme;
  const sparkle = rt.theme.unicode ? "✦" : "*";
  const prompt = model.textOverrides?.prompt ?? "What would you like to do?";
  return [
    "",
    ...banner,
    "",
    "  " + accent(sparkle) + " " + c.whiteBright.bold(prompt) + " " + c.dim("…"),
    "",
  ];
}

/* ── proportional column math (one source of truth) ──────────────────────────── */

function columns(width: number): { labelWidth: number; hintCol: number; hintWidth: number } {
  const labelWidth = clampNum(Math.round((width - 4) * 0.5), 22, 40);
  const hintCol = 4 + labelWidth; // pointer + space + glyph(1) + space, then the label column
  const hintWidth = Math.max(10, width - hintCol - 1);
  return { labelWidth, hintCol, hintWidth };
}

function renderRows(
  rows: Row[],
  state: MenuState,
  width: number,
  bodyHeight: number,
  theme: Theme,
  settings: Settings,
): string[] {
  const out: string[] = [];
  const selectedRow = rows[state.selected];
  const listHeight = Math.max(3, bodyHeight);
  const contentHeight = Math.max(1, listHeight - 2);
  keepSelectedVisible(state, contentHeight, rows.length);
  const visible = rows.slice(state.offset, state.offset + contentHeight);
  const up = theme.unicode ? "↑" : "^";
  const down = theme.unicode ? "↓" : "v";
  out.push(state.offset > 0 ? "  " + theme.c.dim(`${up} ${state.offset} more`) : "");
  for (let i = 0; i < visible.length; i++) {
    out.push(renderRow(visible[i]!, selectedRow, width, theme, settings));
  }
  while (out.length < listHeight - 1) out.push("");
  const hiddenBelow = Math.max(0, rows.length - (state.offset + contentHeight));
  out.push(hiddenBelow > 0 ? "  " + theme.c.dim(`${down} ${hiddenBelow} more`) : "");
  while (out.length < bodyHeight) out.push("");
  return out;
}

function renderRow(row: Row, selectedRow: Row | undefined, width: number, theme: Theme, settings: Settings): string {
  const { c } = theme;

  if (row.kind === "section") {
    return sectionLine(row.label, row.count, width, theme, settings);
  }
  if (row.kind === "empty") return "   " + c.dim(row.label);

  const selected = row === selectedRow;
  const pointer = selected ? c.cyanBright.bold(">") : " ";

  if (row.kind === "category") {
    const meta = sectionMeta(row.key);
    const tone = toneColor(meta.tone, theme);
    const glyphChar = sectionIcon(meta, theme);
    const hint = c.dim(row.preview ? `${row.count} · ${row.preview}` : `${row.count} commands`);
    const labeler = (s: string) => (selected ? tone.bold(s) : tone(s));
    return composeRow(pointer, glyphChar, tone(glyphChar), meta.title, selected, hint, width, theme, labeler);
  }

  if (row.kind === "navig") {
    const meta = sectionMeta("Utilities");
    const glyphChar = theme.unicode ? row.item.glyph : NAVIG_ASCII[row.item.id];
    const glyph = toneColor(meta.tone, theme)(glyphChar);
    return composeRow(pointer, glyphChar, glyph, row.item.label, selected, c.dim(row.item.description), width, theme);
  }

  if (row.kind !== "action") return ""; // folder rows only appear in the tree renderer

  // action row
  const action = row.action;
  const meta = sectionMeta(action.group);
  const danger = action.risk === "dangerous";
  const confirm = action.risk === "confirm";
  const glyphChar = row.recent
    ? theme.unicode ? "↻" : "~"
    : danger
      ? theme.unicode ? "▲" : "!"
      : confirm
        ? theme.unicode ? "□" : "?"
        : theme.unicode ? "◆" : "*";
  const glyph = (danger ? c.redBright : confirm ? c.yellowBright : toneColor(meta.tone, theme))(glyphChar);

  const command = `${action.launcher} ${action.argv.join(" ")}`.trim();
  const base = settings.showDescriptions && action.description ? action.description : settings.showCommandHints ? command : "";
  let hint = "";
  if (settings.showRiskBadges && action.risk !== "safe") {
    hint += danger ? c.red(`${theme.unicode ? "▲" : "!"} danger  `) : c.yellow(`${theme.unicode ? "□" : "?"} confirm  `);
  }
  if (base) hint += c.dim(base);

  return composeRow(pointer, glyphChar, glyph, action.label, selected, hint, width, theme);
}

/**
 * The premium, mathematically-aligned row: pointer · glyph · label (truncated to a fixed column)
 * · hint. The hint always begins at the same column regardless of glyph width, so every command
 * lines up like schema's console.
 */
function composeRow(
  pointer: string,
  glyphChar: string,
  glyph: string,
  labelText: string,
  selected: boolean,
  styledHint: string,
  width: number,
  theme: Theme,
  labelColor?: (s: string) => string,
  lead = "",
  leadW = 0,
): string {
  const { c } = theme;
  const { hintCol, hintWidth } = columns(width - leadW); // columns are relative to after the lead
  const prefixW = 2 + displayWidth(glyphChar) + 1; // pointer+space + glyph + space
  const labelBudget = Math.max(4, hintCol - prefixW);
  const labelClipped = truncatePlain(labelText, labelBudget);
  const label = labelColor
    ? labelColor(labelClipped)
    : selected
      ? c.whiteBright.bold(labelClipped)
      : c.white(labelClipped);
  const used = prefixW + displayWidth(labelClipped);
  const hint = styledHint ? clip(styledHint, hintWidth + 1) : "";
  if (!hint) return clip(`${lead}${pointer} ${glyph} ${label}`, width);
  const pad = Math.max(1, hintCol - used);
  return clip(`${lead}${pointer} ${glyph} ${label}${" ".repeat(pad)}${hint}`, width);
}

/* ── Tree / filesystem layout ──────────────────────────────────────────────────── */

function renderTree(
  rows: Row[],
  state: MenuState,
  width: number,
  bodyHeight: number,
  theme: Theme,
  settings: Settings,
  rootLabel: string,
): string[] {
  const { c, accent } = theme;
  const out: string[] = [];
  const selectedRow = rows[state.selected];
  // Reserve one line for the pinned root node + two for the scroll markers.
  const listHeight = Math.max(3, bodyHeight - 1);
  const contentHeight = Math.max(1, listHeight - 2);
  keepSelectedVisible(state, contentHeight, rows.length);
  const up = theme.unicode ? "↑" : "^";
  const down = theme.unicode ? "↓" : "v";

  // Pinned root — the tree reads as a single filesystem rooted at the project.
  const rootGlyph = theme.emoji ? "📂" : theme.unicode ? "◈" : "#";
  out.push(clip(`  ${accent(rootGlyph)} ${c.whiteBright.bold(rootLabel)}`, width));
  out.push(state.offset > 0 ? "  " + c.dim(`${up} ${state.offset} more`) : "");
  for (let k = 0; k < contentHeight; k++) {
    const idx = state.offset + k;
    out.push(idx < rows.length ? treeLine(rows[idx]!, selectedRow, width, theme, settings) : "");
  }
  while (out.length < listHeight) out.push("");
  const hiddenBelow = Math.max(0, rows.length - (state.offset + contentHeight));
  out.push(hiddenBelow > 0 ? "  " + c.dim(`${down} ${hiddenBelow} more`) : "");
  while (out.length < bodyHeight) out.push("");
  return out.slice(0, bodyHeight);
}

/** Build the dim connector prefix (│  / ├─ / └─) for a row from its precomputed tree geometry. */
function treeLead(meta: TreeMeta, theme: Theme): { lead: string; leadW: number } {
  const { c } = theme;
  const uni = theme.unicode;
  const bar = uni ? "│  " : "|  ";
  const tee = uni ? "├─ " : "+- ";
  const ell = uni ? "└─ " : "`- ";
  const stems = meta.ancestorLast.map((last) => (last ? "   " : bar)).join("");
  const lead = "  " + c.dim(stems + (meta.isLast ? ell : tee));
  const leadW = 2 + 3 * meta.ancestorLast.length + 3;
  return { lead, leadW };
}

function treeLine(
  row: Row,
  selectedRow: Row | undefined,
  width: number,
  theme: Theme,
  settings: Settings,
): string {
  const { c } = theme;
  const uni = theme.unicode;
  if (row.kind === "empty" || row.kind === "category") return "     " + c.dim(row.kind === "empty" ? row.label : row.key);
  const meta = row.tree ?? { depth: 0, ancestorLast: [], isLast: true };
  const { lead, leadW } = treeLead(meta, theme);

  if (row.kind === "section") {
    const isSearch = row.label.startsWith("Search ");
    const sm = sectionMeta(isSearch ? "Misc" : row.label);
    const title = isSearch ? row.label : sm.title;
    const tone = toneColor(sm.tone, theme);
    const count = settings.showCounts && row.count !== undefined ? c.dim(` ${row.count}`) : "";
    return clip(`${lead}${tone(sectionIcon(sm, theme))} ${tone.bold(title)}${count}`, width);
  }

  if (row.kind === "folder") {
    const glyph = uni ? "▸" : ">";
    const count = settings.showCounts && row.count !== undefined ? c.dim(` ${row.count}`) : "";
    return clip(`${lead}${c.dim(glyph)} ${c.whiteBright(row.label)}${count}`, width);
  }

  const selected = row === selectedRow;
  const pointer = selected ? c.cyanBright.bold(">") : " ";

  if (row.kind === "navig") {
    const sm = sectionMeta("Utilities");
    const glyphChar = uni ? row.item.glyph : NAVIG_ASCII[row.item.id];
    const glyph = toneColor(sm.tone, theme)(glyphChar);
    return composeRow(pointer, glyphChar, glyph, row.item.label, selected, c.dim(row.item.description), width, theme, undefined, lead, leadW);
  }
  if (row.kind !== "action") return "";

  const action = row.action;
  const sm = sectionMeta(action.group);
  const danger = action.risk === "dangerous";
  const confirm = action.risk === "confirm";
  const glyphChar = row.recent ? (uni ? "↻" : "~") : danger ? (uni ? "▲" : "!") : confirm ? (uni ? "□" : "?") : uni ? "◆" : "*";
  const glyph = (danger ? c.redBright : confirm ? c.yellowBright : toneColor(sm.tone, theme))(glyphChar);
  const command = `${action.launcher} ${action.argv.join(" ")}`.trim();
  const base = settings.showDescriptions && action.description ? action.description : settings.showCommandHints ? command : "";
  return composeRow(pointer, glyphChar, glyph, action.label, selected, base ? c.dim(base) : "", width, theme, undefined, lead, leadW);
}

function sectionLine(label: string, count: number | undefined, width: number, theme: Theme, settings: Settings): string {
  const { c } = theme;
  const isSearch = label.startsWith("Search ");
  const meta = sectionMeta(isSearch ? "Misc" : label);
  const title = isSearch ? label : meta.title;
  const glyphChar = sectionIcon(meta, theme);
  const tone = toneColor(meta.tone, theme);
  const countText = settings.showCounts && count !== undefined ? ` ${count}` : "";
  const { hintCol } = columns(width);
  const prefixW = 2 + displayWidth(glyphChar) + 1 + displayWidth(title) + countText.length + 1;
  const ruleLen = settings.density === "compact" ? 0 : Math.max(4, hintCol - prefixW);
  const rule = ruleLen ? c.dim((theme.unicode ? "─" : "-").repeat(ruleLen)) : "";
  return clip(`  ${tone(glyphChar)} ${tone.bold(title)}${c.dim(countText)} ${rule}`, width);
}

/* ── Customizer view ─────────────────────────────────────────────────────────── */

type CustEntry =
  | { type: "section"; group: string; total: number; hidden: number }
  | { type: "toggle"; action: Action; selectIndex: number };

function customizerEntries(allActions: Action[], hidden: Set<string>): CustEntry[] {
  const groupsInOrder = [
    ...GROUPS.filter((g) => allActions.some((a) => a.group === g)),
    ...[...new Set(allActions.map((a) => a.group))].filter((g) => !(GROUPS as readonly string[]).includes(g)),
  ];
  const entries: CustEntry[] = [];
  let selectIndex = 0;
  for (const group of groupsInOrder) {
    const items = allActions.filter((a) => a.group === group);
    if (!items.length) continue;
    entries.push({ type: "section", group, total: items.length, hidden: items.filter((a) => hidden.has(a.id)).length });
    for (const action of items) entries.push({ type: "toggle", action, selectIndex: selectIndex++ });
  }
  return entries;
}

function renderCustomizer(
  allActions: Action[],
  hidden: Set<string>,
  state: MenuState,
  width: number,
  bodyHeight: number,
  theme: Theme,
): string[] {
  const { c, accent } = theme;
  const entries = customizerEntries(allActions, hidden);
  const toggleCount = entries.filter((e) => e.type === "toggle").length;
  state.customizerIndex = clampNum(state.customizerIndex ?? 0, 0, Math.max(0, toggleCount - 1));
  state.customizerOffset = state.customizerOffset ?? 0;
  const selectedEntry = entries.findIndex((e) => e.type === "toggle" && e.selectIndex === state.customizerIndex);

  const head = [
    ` ${accent("CUSTOMIZE MENU")}  ${c.dim("— space/enter toggle · ↑/↓ move · esc save · a category with all items hidden disappears")}`,
    "",
  ];
  const listHeight = Math.max(3, bodyHeight - head.length);
  const contentHeight = Math.max(1, listHeight - 2);

  if (selectedEntry < state.customizerOffset) state.customizerOffset = selectedEntry;
  if (selectedEntry >= state.customizerOffset + contentHeight) state.customizerOffset = selectedEntry - contentHeight + 1;
  state.customizerOffset = clampNum(state.customizerOffset, 0, Math.max(0, entries.length - contentHeight));

  const up = theme.unicode ? "↑" : "^";
  const down = theme.unicode ? "↓" : "v";
  const lines: string[] = [...head];
  lines.push(state.customizerOffset > 0 ? "  " + c.dim(`${up} ${state.customizerOffset} more`) : "");
  for (const entry of entries.slice(state.customizerOffset, state.customizerOffset + contentHeight)) {
    if (entry.type === "section") {
      const meta = sectionMeta(entry.group);
      const tone = toneColor(meta.tone, theme);
      const tag = entry.hidden >= entry.total ? c.dim("  (all hidden)") : c.dim(`  ${entry.total - entry.hidden}/${entry.total} shown`);
      lines.push(clip(`  ${tone(sectionIcon(meta, theme))} ${tone.bold(meta.title)}${tag}`, width));
    } else {
      const sel = entry.selectIndex === state.customizerIndex;
      const isHidden = hidden.has(entry.action.id);
      const pointer = sel ? c.cyanBright.bold(">") : " ";
      const boxChar = theme.unicode ? (isHidden ? "☐" : "☑") : isHidden ? "[ ]" : "[x]";
      const box = isHidden ? c.dim(boxChar) : c.green(boxChar);
      const cmd = c.dim(`${entry.action.launcher} ${entry.action.argv.join(" ")}`.trim());
      lines.push(
        composeRow(pointer, boxChar, box, entry.action.label, sel, cmd, width, theme, isHidden ? c.dim : undefined),
      );
    }
  }
  while (lines.length < head.length + listHeight - 1) lines.push("");
  const hiddenBelow = Math.max(0, entries.length - (state.customizerOffset + contentHeight));
  lines.push(hiddenBelow > 0 ? "  " + c.dim(`${down} ${hiddenBelow} more`) : "");
  while (lines.length < bodyHeight) lines.push("");
  return lines.slice(0, bodyHeight);
}

function handleCustomizerKey(key: Keypress, state: MenuState, allActions: Action[], hiddenDraft: Set<string>): boolean {
  const entries = customizerEntries(allActions, hiddenDraft);
  const toggles = entries.filter((e): e is Extract<CustEntry, { type: "toggle" }> => e.type === "toggle");
  const count = toggles.length;
  if (!count) return false;
  if (key.name === "up") {
    state.customizerIndex = (state.customizerIndex - 1 + count) % count;
    return true;
  }
  if (key.name === "down") {
    state.customizerIndex = (state.customizerIndex + 1) % count;
    return true;
  }
  if (key.name === "pageup") {
    state.customizerIndex = Math.max(0, state.customizerIndex - 8);
    return true;
  }
  if (key.name === "pagedown") {
    state.customizerIndex = Math.min(count - 1, state.customizerIndex + 8);
    return true;
  }
  if (key.name === "home") {
    state.customizerIndex = 0;
    return true;
  }
  if (key.name === "end") {
    state.customizerIndex = count - 1;
    return true;
  }
  if (key.name === "space") {
    const action = toggles.find((t) => t.selectIndex === state.customizerIndex)?.action;
    if (action) (hiddenDraft.has(action.id) ? hiddenDraft.delete(action.id) : hiddenDraft.add(action.id));
    return true;
  }
  return false;
}

/* ── Plugin manager view ─────────────────────────────────────────────────────── */

type PMEntry =
  | { type: "plugin"; summary: PluginSummary; selectIndex: number }
  | { type: "setting"; pluginId: string; setting: ResolvedPluginSetting; selectIndex: number };

function pluginEntries(model: MenuModel, draft: Map<string, boolean | string>): PMEntry[] {
  const entries: PMEntry[] = [];
  let i = 0;
  for (const p of model.loadedPlugins ?? []) {
    entries.push({ type: "plugin", summary: p, selectIndex: i++ });
    const active = draft.has(p.id) ? draft.get(p.id) === true : p.active;
    if (active) {
      for (const s of model.pluginSettings ?? []) {
        if (s.pluginId === p.id) entries.push({ type: "setting", pluginId: p.id, setting: s, selectIndex: i++ });
      }
    }
  }
  return entries;
}

function renderPluginManager(
  model: MenuModel,
  draft: Map<string, boolean | string>,
  state: MenuState,
  width: number,
  bodyHeight: number,
  theme: Theme,
): string[] {
  const { c, accent } = theme;
  const entries = pluginEntries(model, draft);
  const count = entries.length;
  state.pluginIndex = clampNum(state.pluginIndex ?? 0, 0, Math.max(0, count - 1));
  state.pluginOffset = state.pluginOffset ?? 0;

  const head = [
    ` ${accent("PLUGINS")}  ${c.dim("— space toggle · ↑/↓ move · esc save · built-in + .navig/plugins + npm")}`,
    "",
  ];
  const listHeight = Math.max(3, bodyHeight - head.length);
  const contentHeight = Math.max(1, listHeight - 2);
  if (state.pluginIndex < state.pluginOffset) state.pluginOffset = state.pluginIndex;
  if (state.pluginIndex >= state.pluginOffset + contentHeight) state.pluginOffset = state.pluginIndex - contentHeight + 1;
  state.pluginOffset = clampNum(state.pluginOffset, 0, Math.max(0, count - contentHeight));

  const up = theme.unicode ? "↑" : "^";
  const down = theme.unicode ? "↓" : "v";
  const lines: string[] = [...head];
  if (!count) lines.push("   " + c.dim("no plugins found — drop one in .navig/plugins/ (see docs/plugins.md)"));
  lines.push(state.pluginOffset > 0 ? "  " + c.dim(`${up} ${state.pluginOffset} more`) : "");
  for (const entry of entries.slice(state.pluginOffset, state.pluginOffset + contentHeight)) {
    const sel = entry.selectIndex === state.pluginIndex;
    const pointer = sel ? c.cyanBright.bold(">") : " ";
    if (entry.type === "plugin") {
      const p = entry.summary;
      const active = draft.has(p.id) ? draft.get(p.id) === true : p.active;
      const boxChar = theme.unicode ? (active ? "☑" : "☐") : active ? "[x]" : "[ ]";
      const box = p.error ? c.red(boxChar) : active ? c.green(boxChar) : c.dim(boxChar);
      const tags = [p.tier, p.origin, p.version ? `v${p.version}` : "", active && p.auto ? "auto" : "", p.error ? "error" : ""].filter(Boolean);
      lines.push(composeRow(pointer, boxChar, box, p.id, sel, c.dim(tags.join(" · ")), width, theme, active ? undefined : c.dim));
    } else {
      const s = entry.setting;
      const key = `${s.pluginId}::${s.key}`;
      const val = draft.has(key) ? draft.get(key)! : s.value;
      const on = val === true;
      const isToggle = s.type === "toggle";
      const boxChar = isToggle ? (theme.unicode ? (on ? "☑" : "☐") : on ? "[x]" : "[ ]") : theme.unicode ? "◦" : "-";
      const box = isToggle ? (on ? c.green(boxChar) : c.dim(boxChar)) : c.dim(boxChar);
      const hint = isToggle ? "" : accent(String(val));
      lines.push(composeRow(pointer, boxChar, box, `  ${s.label}`, sel, hint, width, theme));
    }
  }
  while (lines.length < head.length + listHeight - 1) lines.push("");
  const hiddenBelow = Math.max(0, count - (state.pluginOffset + contentHeight));
  lines.push(hiddenBelow > 0 ? "  " + c.dim(`${down} ${hiddenBelow} more`) : "");
  while (lines.length < bodyHeight) lines.push("");
  return lines.slice(0, bodyHeight);
}

function handlePluginKey(key: Keypress, state: MenuState, model: MenuModel, draft: Map<string, boolean | string>): boolean {
  const entries = pluginEntries(model, draft);
  const count = entries.length;
  if (!count) return false;
  if (key.name === "up") return (state.pluginIndex = (state.pluginIndex - 1 + count) % count), true;
  if (key.name === "down") return (state.pluginIndex = (state.pluginIndex + 1) % count), true;
  if (key.name === "pageup") return (state.pluginIndex = Math.max(0, state.pluginIndex - 8)), true;
  if (key.name === "pagedown") return (state.pluginIndex = Math.min(count - 1, state.pluginIndex + 8)), true;
  if (key.name === "home") return (state.pluginIndex = 0), true;
  if (key.name === "end") return (state.pluginIndex = count - 1), true;
  if (key.name === "space") {
    const entry = entries.find((e) => e.selectIndex === state.pluginIndex);
    if (entry?.type === "plugin") {
      const cur = draft.has(entry.summary.id) ? draft.get(entry.summary.id) === true : entry.summary.active;
      draft.set(entry.summary.id, !cur);
    } else if (entry?.type === "setting") {
      const s = entry.setting;
      const k = `${s.pluginId}::${s.key}`;
      const cur = draft.has(k) ? draft.get(k)! : s.value;
      if (s.type === "toggle") draft.set(k, !(cur === true));
      else if (s.values?.length) {
        const idx = Math.max(0, s.values.indexOf(String(cur)));
        draft.set(k, s.values[(idx + 1) % s.values.length]!);
      }
    }
    return true;
  }
  return false;
}

function renderAbout(model: MenuModel, rt: MenuRenderRuntime, width: number, bodyHeight: number, theme: Theme): string[] {
  const { c, accent } = theme;
  const lines: string[] = [
    ` ${accent("ABOUT")}`,
    "",
    kv("navig-menu", `v${rt.toolVersion ?? rt.manifest.tool.version}`, c),
    kv("website", PROJECT_URL, c),
    kv("author", AUTHOR_GITHUB, c),
    kv("license", LICENSE, c),
    "",
    ` ${accent("PLUGINS")}`,
    ...(model.loadedPlugins?.length
      ? model.loadedPlugins.map((p) => {
          const state = p.error ? c.red("error") : p.active ? c.green("active") : c.dim("off");
          const tags = [p.tier, p.origin, p.version ? `v${p.version}` : ""].filter(Boolean).join(" · ");
          return `  ${accent(p.id.padEnd(14))} ${c.dim(tags)}  ${state}`;
        })
      : ["  " + c.dim("no plugins loaded")]),
    ...(model.about?.length ? ["", ` ${accent("NOTES")}`, ...model.about.map((l) => "  " + c.dim(l))] : []),
  ].map((line) => clip(line, width));
  while (lines.length < bodyHeight) lines.push("");
  return lines.slice(0, bodyHeight);
}

function renderDetails(model: MenuModel, rt: MenuRenderRuntime, width: number, bodyHeight: number, theme: Theme): string[] {
  const { c, accent, sym } = theme;
  const manifest = rt.manifest;
  const lines: string[] = [
    ` ${accent("DOCTOR — environment & detection")}`,
    "",
    kv("tool", `${manifest.tool.name} v${rt.toolVersion ?? manifest.tool.version}`, c),
    kv("node", `${process.version} · ${process.platform}/${process.arch}`, c),
    kv("root", model.root, c),
    kv("package manager", `${model.packageManager} (${model.pmConfidence})`, c),
    kv("workspace", `${model.workspaceKind} · ${model.packages.length} packages`, c),
    kv("frameworks", model.frameworks.join(", ") || "none", c),
    kv("scripts", String(model.stats.scripts), c),
    "",
    ` ${accent("ENDPOINTS")}`,
    ...(model.endpoints.length
      ? model.endpoints.map((ep) => `  ${accent(ep.label.padEnd(14))} ${c.white(ep.url)}${ep.tls ? c.green("  TLS ✓") : ""}`)
      : ["  " + c.dim("no local endpoints detected")]),
    "",
    ` ${accent("SERVICES")}`,
    ...(manifest.services.length
      ? manifest.services.map((service) => {
          const ev = service.evidence.map((e) => e.detail ?? e.file).filter(Boolean).join(` ${sym.dot} `);
          return `  ${accent(service.id.padEnd(14))} ${c.dim(service.confidence)} ${c.dim(ev)}`;
        })
      : ["  " + c.dim("no services detected")]),
    "",
    ` ${accent("MENU CONTRACT")}`,
    `  ${c.dim(definitionPath(model.root))}`,
  ].map((line) => clip(line, width));
  while (lines.length < bodyHeight) lines.push("");
  return lines.slice(0, bodyHeight);
}

function renderHelp(width: number, bodyHeight: number, theme: Theme): string[] {
  const { c, accent } = theme;
  const lines = [
    ` ${accent("KEYS")}`,
    "",
    `  ${c.white("↑ / ↓")}         move one command`,
    `  ${c.white("PgUp/PgDn")}     move by viewport`,
    `  ${c.white("Home/End")}      jump to first / last`,
    `  ${c.white("enter")}         run selected (or open a UTILITIES action)`,
    `  ${c.white("/ or type")}     search commands`,
    `  ${c.white("esc")}           clear search / close panels`,
    `  ${c.white("s  or  ,")}      settings`,
    `  ${c.white("c")}             customize items (hide / show)`,
    `  ${c.white("n")}             add / edit a pre-run note (logins, reminders)`,
    `  ${c.white("p")}             plugins (activate / deactivate)`,
    `  ${c.white("g")}             regenerate .navig/menu.json`,
    `  ${c.white("r")}             refresh scan`,
    `  ${c.white("d")}             doctor / details`,
    `  ${c.white("a")}             about`,
    `  ${c.white("?")}             this help`,
    `  ${c.white("q")}             quit`,
  ].map((line) => clip(line, width));
  while (lines.length < bodyHeight) lines.push("");
  return lines.slice(0, bodyHeight);
}

/* ── Settings view ───────────────────────────────────────────────────────────── */

interface SettingField {
  label?: string;
  value?: (s: Settings) => string;
  change?: (s: Settings, dir: 1 | -1) => void;
  open?: View;
  /** Fires a runtime action instead of changing a value. */
  action?: "regenerate" | "refresh" | "edit" | "clearRecents" | "organize";
  /** One-line help shown in the footer while this field is selected. */
  help?: string;
  /** A non-selectable section divider. */
  heading?: string;
}

const GLYPH_CYCLE: GlyphSetting[] = ["auto", "emoji", "unicode", "ascii"];

const SETTING_FIELDS: SettingField[] = [
  { heading: "Look" },
  { label: "Accent", value: (s) => s.accent, change: (s, d) => (s.accent = cycle(ACCENT_NAMES as Settings["accent"][], s.accent, d)), help: "the highlight colour used across the menu" },
  { label: "Icons", value: (s) => s.glyphs, change: (s, d) => (s.glyphs = cycle(GLYPH_CYCLE, s.glyphs, d)), help: "glyph style: auto · emoji · unicode · ascii (default)" },
  { label: "Layout", value: (s) => ({ flat: "sections", list: "list", categories: "categories", tree: "tree", projects: "by project" }[s.layout]), change: (s, d) => (s.layout = cycle(["flat", "list", "categories", "tree", "projects"] as Settings["layout"][], s.layout, d)), help: "sections = grouped · list = flat · categories = drill-in · tree = nested · by project = grouped per app" },
  { label: "Banner", value: (s) => s.bannerStyle, change: (s, d) => (s.bannerStyle = cycle(["cosmic", "console"] as Settings["bannerStyle"][], s.bannerStyle, d)), help: "cosmic = rich rows · console = one compact status line" },
  { label: "Density", value: (s) => s.density, change: (s, d) => (s.density = cycle(["comfortable", "compact"] as Settings["density"][], s.density, d)), help: "comfortable = section rules + breathing room · compact = tight" },
  { heading: "List" },
  { label: "Show descriptions", value: (s) => onOff(s.showDescriptions), change: (s) => (s.showDescriptions = !s.showDescriptions), help: "show a one-line description per command when available" },
  { label: "Show commands", value: (s) => onOff(s.showCommandHints), change: (s) => (s.showCommandHints = !s.showCommandHints), help: "show the shell command on each row (when no description)" },
  { label: "Show counts", value: (s) => onOff(s.showCounts), change: (s) => (s.showCounts = !s.showCounts), help: "show the item count next to each section title" },
  { label: "Show risk badges", value: (s) => onOff(s.showRiskBadges), change: (s) => (s.showRiskBadges = !s.showRiskBadges), help: "inline confirm / danger tags on risky commands" },
  { label: "Show recents", value: (s) => onOff(s.showRecents), change: (s) => (s.showRecents = !s.showRecents), help: "a Recent section of commands you've launched" },
  { label: "Recents shown", value: (s) => String(s.recentLimit), change: (s, d) => (s.recentLimit = clampNum(s.recentLimit + d, 0, 20)), help: "how many recent commands to list (0 = off)" },
  { label: "Utilities rail", value: (s) => onOff(s.showNavigSection), change: (s) => (s.showNavigSection = !s.showNavigSection), help: "the Settings · About · Quit rail at the bottom" },
  { label: "Mouse wheel scroll", value: (s) => onOff(s.mouse), change: (s) => (s.mouse = !s.mouse), help: "scroll the list with the wheel (captures the mouse; hold Shift to select text) · restart to apply" },
  { heading: "Banner rows" },
  { label: "Spaced title", value: (s) => onOff(s.banner.spacedTitle), change: (s) => (s.banner.spacedTitle = !s.banner.spacedTitle), help: "S P A C E the project name in the banner" },
  { label: "Endpoints", value: (s) => onOff(s.banner.endpoints), change: (s) => (s.banner.endpoints = !s.banner.endpoints), help: "the web / API URLs this project serves" },
  { label: "Stack", value: (s) => onOff(s.banner.stack), change: (s) => (s.banner.stack = !s.banner.stack), help: "package manager · frameworks · services chips" },
  { label: "Branch", value: (s) => onOff(s.banner.git), change: (s) => (s.banner.git = !s.banner.git), help: "git branch · sha · dirty count" },
  { label: "Runtime", value: (s) => onOff(s.banner.runtime), change: (s) => (s.banner.runtime = !s.banner.runtime), help: "node version + platform" },
  { label: "Date", value: (s) => onOff(s.banner.date), change: (s) => (s.banner.date = !s.banner.date), help: "current date + time" },
  { label: "Packages", value: (s) => onOff(s.banner.packages), change: (s) => (s.banner.packages = !s.banner.packages), help: "workspace package names" },
  { label: "Stats", value: (s) => onOff(s.banner.stats), change: (s) => (s.banner.stats = !s.banner.stats), help: "scripts · packages · docs · services counts" },
  { label: "Plugin lines", value: (s) => onOff(s.banner.plugins), change: (s) => (s.banner.plugins = !s.banner.plugins), help: "banner lines contributed by plugins (e.g. store stats)" },
  { heading: "Actions" },
  { label: "Customize items →", open: "customizer", help: "hide / show individual commands" },
  { label: "Plugins →", open: "plugins", help: "activate / deactivate plugins + their options" },
  { label: "Doctor / details →", open: "details", help: "environment, stack, services, endpoints" },
  { label: "Regenerate menu →", action: "regenerate", help: "rewrite .navig/menu.json from a fresh scan" },
  { label: "Organize with AI →", action: "organize", help: "AI: describe every command + report gaps (needs a key)" },
  { label: "Refresh scan →", action: "refresh", help: "re-detect scripts, packages, endpoints" },
  { label: "Clear recents →", action: "clearRecents", help: "forget the commands you've recently launched" },
  { label: "Edit menu.json →", action: "edit", help: "show the path to the menu contract" },
];

function sampleAction(id: string, label: string, tokens: string[], group: string, risk: Action["risk"], description?: string): Action {
  return {
    id,
    label,
    description,
    group,
    launcher: tokens[0] ?? "",
    argv: tokens.slice(1),
    cwd: ".",
    risk,
    longRunning: false,
    confidence: "explicit",
    evidence: [],
    source: "sample",
  };
}

/** A tiny live sample rendered with the DRAFT settings/theme — so style/density/glyphs/layout preview. */
function renderSettingsPreview(draft: Settings, theme: Theme, width: number): string[] {
  const { c } = theme;
  const dash = theme.unicode ? "─" : "-";
  const rule = c.dim(dash.repeat(Math.max(8, Math.min(width, 60) - 12)));
  const section: Row = { kind: "section", label: "Development", count: 2 };
  const dev: Row = { kind: "action", action: sampleAction("dev", "Dev", ["pnpm", "run", "dev"], "Development", "safe", "start the dev server") };
  const deploy: Row = { kind: "action", action: sampleAction("deploy", "Deploy", ["pnpm", "run", "deploy"], "Deploy", "dangerous") };
  const header = "  " + c.dim(`preview · ${draft.layout} · ${draft.glyphs} · ${draft.bannerStyle}`) + "  " + rule;
  let body: string[];
  if (draft.layout === "tree") {
    // A tiny nested sample so the folders-in-folders shape is visible while editing.
    const sec: Row = { ...section, tree: { depth: 0, ancestorLast: [], isLast: true } };
    const devLeaf: Row = { ...dev, tree: { depth: 1, ancestorLast: [true], isLast: false } };
    const folder: Row = { kind: "folder", label: "db", count: 1, tree: { depth: 1, ancestorLast: [true], isLast: true } };
    const nested: Row = { ...deploy, tree: { depth: 2, ancestorLast: [true, true], isLast: true } };
    body = [sec, devLeaf, folder, nested].map((r) => treeLine(r, devLeaf, width, theme, draft));
  } else {
    body = [
      renderRow(section, undefined, width, theme, draft),
      renderRow(dev, dev, width, theme, draft),
      renderRow(deploy, dev, width, theme, draft),
    ];
  }
  return [header, ...body, "  " + rule, ""];
}

function firstSelectableSetting(): number {
  return Math.max(0, SETTING_FIELDS.findIndex((f) => !f.heading));
}

function stepSetting(idx: number, dir: 1 | -1): number {
  const n = SETTING_FIELDS.length;
  let i = idx;
  for (let k = 0; k < n; k++) {
    i = (i + dir + n) % n;
    if (!SETTING_FIELDS[i]!.heading) return i;
  }
  return idx;
}

function renderSettings(draft: Settings, state: MenuState, width: number, bodyHeight: number, theme: Theme): string[] {
  const { c, accent } = theme;
  if (SETTING_FIELDS[state.settingsIndex]?.heading) state.settingsIndex = firstSelectableSetting();
  // A live preview so style / density / glyphs / layout changes are visible while you edit.
  const preview = bodyHeight >= 16 ? renderSettingsPreview(draft, theme, width) : [];
  const head = [...preview, ` ${accent("SETTINGS")}  ${c.dim("— ←/→ change · ↑/↓ move · enter open/run · esc save")}`, ""];
  const listHeight = Math.max(3, bodyHeight - head.length);
  const contentHeight = Math.max(1, listHeight - 2);
  const index = state.settingsIndex;
  if (index < (state.settingsOffset ?? 0)) state.settingsOffset = index;
  if (index >= (state.settingsOffset ?? 0) + contentHeight) state.settingsOffset = index - contentHeight + 1;
  state.settingsOffset = clampNum(state.settingsOffset ?? 0, 0, Math.max(0, SETTING_FIELDS.length - contentHeight));

  const up = theme.unicode ? "↑" : "^";
  const down = theme.unicode ? "↓" : "v";
  const dash = theme.unicode ? "─" : "-";
  const lines = [...head];
  lines.push(state.settingsOffset > 0 ? "  " + c.dim(`${up} ${state.settingsOffset} more`) : "");
  for (let i = state.settingsOffset; i < Math.min(SETTING_FIELDS.length, state.settingsOffset + contentHeight); i++) {
    const field = SETTING_FIELDS[i]!;
    if (field.heading) {
      lines.push("  " + c.dim(`${dash} ${field.heading} ${dash.repeat(Math.max(2, 22 - field.heading.length))}`));
      continue;
    }
    const sel = i === index;
    const pointer = sel ? c.cyanBright.bold(">") : " ";
    const label = sel ? c.whiteBright.bold(field.label) : c.white(field.label);
    const value = field.open || field.action ? c.dim("enter →") : accent(field.value!(draft));
    const left = `${pointer} ${label}`;
    const pad = Math.max(2, 34 - displayWidth(stripAnsi(left)));
    lines.push(clip(`${left}${" ".repeat(pad)}${value}`, width));
  }
  while (lines.length < head.length + listHeight - 1) lines.push("");
  const hiddenBelow = Math.max(0, SETTING_FIELDS.length - (state.settingsOffset + contentHeight));
  lines.push(hiddenBelow > 0 ? "  " + c.dim(`${down} ${hiddenBelow} more`) : "");
  while (lines.length < bodyHeight) lines.push("");
  return lines.slice(0, bodyHeight);
}

type SettingsSignal = "changed" | `open:${View}` | `action:${string}` | false;

function handleSettingsKey(key: Keypress, state: MenuState, draft: Settings): SettingsSignal {
  if (SETTING_FIELDS[state.settingsIndex]?.heading) state.settingsIndex = firstSelectableSetting();
  if (key.name === "up") return (state.settingsIndex = stepSetting(state.settingsIndex, -1)), "changed";
  if (key.name === "down") return (state.settingsIndex = stepSetting(state.settingsIndex, 1)), "changed";
  if (key.name === "home") return (state.settingsIndex = firstSelectableSetting()), "changed";
  if (key.name === "end") return (state.settingsIndex = stepSetting(0, -1)), "changed";
  const field = SETTING_FIELDS[state.settingsIndex]!;
  if (key.name === "right" || key.name === "space" || key.name === "return") {
    if (field.open) return `open:${field.open}`;
    if (field.action) return `action:${field.action}`;
  }
  if (key.name === "left" && field.change) {
    field.change(draft, -1);
    return "changed";
  }
  if ((key.name === "right" || key.name === "space") && field.change) {
    field.change(draft, 1);
    return "changed";
  }
  return false;
}

function renderFooter(
  model: MenuModel,
  state: MenuState,
  rows: Row[],
  width: number,
  theme: Theme,
  settings: Settings,
): string[] {
  const { c, sym } = theme;
  const search = state.filter ? theme.accent(` search: ${state.filter}`) : c.dim(" type to search");
  const msg = state.message ? ` ${sym.dot} ${state.message}` : "";
  const custom = settings.footer ? ` ${sym.dot} ${settings.footer}` : "";
  const divider = theme.unicode ? "─" : "-";
  const upDown = theme.unicode ? "↑/↓" : "up/down";
  const row = rows[state.selected];
  const selected = selectedAction(rows, state.selected);
  const selectedNav = row?.kind === "navig" ? row.item : undefined;
  // In Settings, explain the highlighted option; elsewhere describe the selected command.
  const detail =
    state.view === "settings"
      ? SETTING_FIELDS[state.settingsIndex]?.help
      : row?.kind === "category"
        ? row.preview || `${row.count} commands`
        : selected?.description ?? selectedNav?.description;
  const cmd = state.view === "menu" && selected ? `${selected.launcher} ${selected.argv.join(" ")}`.trim() : undefined;
  // In category layout, an entered category shows a breadcrumb + the verb changes to "open".
  const inCategory = settings.layout === "categories" && !!state.category && !state.filter;
  const atPicker = settings.layout === "categories" && !state.category && !state.filter;
  const crumb = inCategory ? `  ${theme.accent(sym.back + " " + sectionMeta(state.category!).title)}` : "";
  const verb = atPicker ? "enter open" : "enter run";
  const back = inCategory ? ` ${sym.dot} esc back` : "";
  return [
    "",
    detail ? "  " + c.dim(truncate(detail, width - 4)) : crumb,
    cmd ? "  " + c.dim(`$ ${truncate(cmd, width - 6)}`) : "",
    "",
    c.dim(divider.repeat(width)),
    clip(` ${upDown} move ${sym.dot} ${verb}${back} ${sym.dot} / search ${sym.dot} s settings ${sym.dot} c customize ${sym.dot} q quit${custom}`, width),
    clip(`${search}${c.dim(msg)}`, width),
  ];
}

/* ── tone + glyph resolution ───────────────────────────────────────────────────── */

function toneColor(tone: ToneKey, theme: Theme): Theme["c"] {
  const { c } = theme;
  switch (tone) {
    case "blue":
      return c.blueBright;
    case "yellow":
      return c.yellowBright;
    case "cyan":
      return c.cyanBright;
    case "green":
      return c.greenBright;
    case "red":
      return c.redBright;
    case "magenta":
      return c.magentaBright;
    case "purple":
      return c.magenta;
    default:
      return accentChalk(theme);
  }
}

/** The theme's accent as a full ChalkInstance (so callers get `.bold` etc.). */
function accentChalk(theme: Theme): Theme["c"] {
  const { c } = theme;
  switch (theme.accentName) {
    case "blue":
      return c.blueBright;
    case "green":
      return c.greenBright;
    case "magenta":
      return c.magentaBright;
    case "purple":
      return c.magenta;
    case "yellow":
      return c.yellowBright;
    case "red":
      return c.redBright;
    default:
      return c.cyanBright;
  }
}

function sectionIcon(meta: ReturnType<typeof sectionMeta>, theme: Theme): string {
  return theme.emoji ? meta.emoji : theme.unicode ? meta.unicode : meta.ascii;
}

function glyphsOf(s: Settings): GlyphStyle | undefined {
  return s.glyphs === "auto" ? undefined : s.glyphs;
}

/* ── shared helpers ─────────────────────────────────────────────────────────────── */

function normalizeSelection(state: MenuState, rows: Row[]): void {
  if (!rows.length) {
    state.selected = 0;
    state.offset = 0;
    return;
  }
  if (!isSelectable(rows[state.selected])) moveToEdge(state, rows, "first");
  if (!isSelectable(rows[state.selected])) state.selected = 0;
  state.selected = Math.max(0, Math.min(rows.length - 1, state.selected));
}

function keepSelectedVisible(state: MenuState, bodyHeight: number, totalRows: number): void {
  if (state.selected < state.offset) state.offset = state.selected;
  if (state.selected >= state.offset + bodyHeight) state.offset = state.selected - bodyHeight + 1;
  state.offset = Math.max(0, Math.min(state.offset, Math.max(0, totalRows - bodyHeight)));
}

function moveSelection(state: MenuState, rows: Row[], delta: number): void {
  if (!rows.some(isSelectable)) return;
  const dir = delta < 0 ? -1 : 1;
  let remaining = Math.abs(delta);
  let idx = state.selected;
  while (remaining > 0) {
    const next = nextSelectable(rows, idx, dir);
    if (next === idx) {
      // Hit the boundary. A single arrow step wraps to the opposite edge (top↔bottom) so long
      // menus loop around, as expected in a keyboard TUI; page jumps still clamp.
      if (Math.abs(delta) === 1) {
        const wrapped = edgeSelectable(rows, dir === 1 ? "first" : "last");
        if (wrapped !== -1) idx = wrapped;
      }
      break;
    }
    idx = next;
    remaining--;
  }
  state.selected = idx;
}

/** First (or last) selectable row index, or -1 if none. Shared by wrap + Home/End. */
function edgeSelectable(rows: Row[], edge: "first" | "last"): number {
  const start = edge === "first" ? 0 : rows.length - 1;
  const dir = edge === "first" ? 1 : -1;
  let idx = start;
  while (idx >= 0 && idx < rows.length && !isSelectable(rows[idx])) idx += dir;
  return idx >= 0 && idx < rows.length ? idx : -1;
}

function moveToEdge(state: MenuState, rows: Row[], edge: "first" | "last"): void {
  const idx = edgeSelectable(rows, edge);
  if (idx !== -1) state.selected = idx;
}

function nextSelectable(rows: Row[], current: number, dir: number): number {
  let idx = current + dir;
  while (idx >= 0 && idx < rows.length) {
    if (isSelectable(rows[idx])) return idx;
    idx += dir;
  }
  return current;
}

function exitCategory(state: MenuState): void {
  state.category = null;
  state.selected = state.pickerIndex;
  state.offset = 0;
}

/** Reset navigation after the model reloads (refresh / regenerate). */
function resetNav(state: MenuState): void {
  state.selected = 0;
  state.offset = 0;
  state.category = null;
}

function selectedAction(rows: Row[], selected: number): Action | undefined {
  const row = rows[selected];
  return row?.kind === "action" ? row.action : undefined;
}

function isSelectable(row: Row | undefined): boolean {
  return row?.kind === "action" || row?.kind === "navig" || row?.kind === "category";
}

function matchScore(action: Action, q: string): number {
  const command = `${action.launcher} ${action.argv.join(" ")}`.trim().toLowerCase();
  const fields = [action.label, action.id, action.canonical, action.description, action.group, command]
    .filter((value): value is string => Boolean(value))
    .map((value) => value.toLowerCase());
  let score = 0;
  for (const field of fields) {
    if (field === q) score += 100;
    else if (field.startsWith(q)) score += 50;
    else if (field.includes(q)) score += 10;
  }
  return score;
}

function appendFilter(state: MenuState, key: Keypress): boolean {
  if (!key.str || key.ctrl || key.str.length !== 1) return false;
  const code = key.str.charCodeAt(0);
  if (code < 32 || code === 127) return false;
  state.filter += key.str;
  state.selected = 0;
  state.offset = 0;
  return true;
}

function isQuit(key: Keypress): boolean {
  return key.name === "q";
}

function pageSize(): number {
  return Math.max(4, Math.floor(((process.stdout.rows ?? 34) - 16) * 0.8));
}

function uiWidth(): number {
  return Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, (process.stdout.columns ?? 100) - 4));
}

function kv(label: string, value: string, c: Theme["c"]): string {
  return `  ${c.dim(label.padEnd(16))} ${value}`;
}

function cycle<T>(values: T[], current: T, dir: 1 | -1): T {
  const i = values.indexOf(current);
  const base = i < 0 ? 0 : i;
  return values[(base + dir + values.length) % values.length]!;
}

function onOff(v: boolean): string {
  return v ? "on" : "off";
}

function clampNum(n: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, n));
}

function truncate(value: string, width: number): string {
  const cleanWidth = Math.max(1, width);
  return displayWidth(value) > cleanWidth ? clip(value, cleanWidth) : value;
}

function truncatePlain(text: string, budget: number): string {
  const max = Math.max(1, budget);
  return text.length > max ? text.slice(0, max - 1) + "…" : text;
}

function stripAnsi(text: string): string {
  return text.replace(/\x1b\[[0-9;]*m/g, "");
}

/** Visible width that counts emoji / wide glyphs as 2 columns and zero-width marks as 0. */
function displayWidth(text: string): number {
  let width = 0;
  for (const ch of stripAnsi(text)) {
    const cp = ch.codePointAt(0)!;
    if (cp === 0xfe0f || cp === 0x200d || (cp >= 0x300 && cp <= 0x36f)) continue; // VS16 / ZWJ / combining
    width += isWide(cp) ? 2 : 1;
  }
  return width;
}

function isWide(cp: number): boolean {
  return (
    (cp >= 0x1100 && cp <= 0x115f) || // Hangul Jamo
    (cp >= 0x2e80 && cp <= 0xa4cf && cp !== 0x303f) || // CJK
    (cp >= 0xac00 && cp <= 0xd7a3) || // Hangul syllables
    (cp >= 0xf900 && cp <= 0xfaff) || // CJK compat
    (cp >= 0xfe30 && cp <= 0xfe4f) || // CJK compat forms
    (cp >= 0xff00 && cp <= 0xff60) || // Fullwidth
    (cp >= 0xffe0 && cp <= 0xffe6) ||
    (cp >= 0x1f000 && cp <= 0x1faff) || // emoji & symbols
    (cp >= 0x2600 && cp <= 0x27bf) // misc symbols & dingbats
  );
}

function clip(text: string, width: number): string {
  const target = Math.max(1, width);
  let out = "";
  let visible = 0;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]!;
    if (ch === "\x1b") {
      const match = /\x1b\[[0-9;]*m/.exec(text.slice(i));
      if (match?.index === 0) {
        out += match[0];
        i += match[0].length - 1;
        continue;
      }
    }
    const cp = text.codePointAt(i)!;
    const w = cp === 0xfe0f || cp === 0x200d ? 0 : isWide(cp) ? 2 : 1;
    if (visible + w > target - 1) return out + "…";
    out += ch;
    if (cp > 0xffff) {
      out += text[i + 1] ?? "";
      i++;
    }
    visible += w;
  }
  return out;
}

async function pause(theme: Theme, msg?: string): Promise<void> {
  if (msg) console.log("  " + theme.c.dim(msg));
  await input(theme.c.dim("  press enter "));
}

/**
 * After a failed action, show a diagnosis and — when one is available — offer a single
 * confirm-gated fix (e.g. install missing deps in the right sub-dir). Never auto-runs.
 */
async function offerDiagnosis(
  action: Action,
  res: RunResult,
  root: string,
  theme: Theme,
): Promise<void> {
  const dx = diagnose(action, root, res.exitCode, res.stderrTail);
  const { c, sym } = theme;

  // Unknown failure → optional AI fallback (opt-in + cost-aware; never automatic).
  if (!dx) {
    if (res.stderrTail.trim()) await offerAiDiagnosis(action, res, root, theme);
    return;
  }

  console.log("\n  " + c.yellow(`${sym.warning} ${dx.title}`));
  console.log("  " + c.dim(dx.hint));
  if (!dx.fix) return;

  const cmd = `${dx.fix.launcher} ${dx.fix.argv.join(" ")}`.trim();
  console.log("  " + c.dim("suggested fix: ") + c.cyan(cmd));
  const ok = await confirmPrompt(`Run ${c.cyan(cmd)} in ${dx.fix.label.replace(/^Install dependencies in /, "")}?`, false);
  if (ok !== true) return;

  const fixAction: Action = {
    ...action,
    id: "diagnose:fix",
    label: dx.fix.label,
    launcher: dx.fix.launcher,
    argv: dx.fix.argv,
    cwd: dx.fix.cwd,
    risk: "safe",
    longRunning: false,
    why: "auto-suggested fix",
  };
  console.log("");
  const fixRes = await runActionLocal(fixAction, root);
  if (fixRes.exitCode === 0) {
    console.log("\n  " + c.green(`${sym.complete} fix applied — re-run ${c.white(action.label)} to continue`));
  } else {
    console.log("\n  " + c.red(`${sym.failed} fix exited ${fixRes.exitCode} — see output above`));
  }
}

/**
 * Fallback for errors the deterministic rules don't recognize. Opt-in and cost-aware: with an AI
 * key available we ask (y/N) before spending a call; without one we offer a paste-ready prompt.
 * AI output is advice — any suggested command is shown for review, never auto-run.
 */
async function offerAiDiagnosis(
  action: Action,
  res: RunResult,
  root: string,
  theme: Theme,
): Promise<void> {
  const { c, sym } = theme;
  if (aiAvailable()) {
    const ok = await confirmPrompt("Ask AI to diagnose this failure?", false);
    if (ok !== true) return;
    console.log("  " + c.dim("asking AI…"));
    const ai = await aiDiagnose(action, root, res.exitCode, res.stderrTail);
    if (!ai) {
      console.log("  " + c.dim("AI couldn't diagnose this — check the output above."));
      return;
    }
    console.log("\n  " + c.yellow(`${sym.warning} ${ai.title}`) + c.dim("  · AI"));
    console.log("  " + c.dim(ai.hint));
    if (ai.command) {
      console.log("  " + c.dim("suggested: ") + c.cyan(ai.command) + c.dim("  (review before running)"));
    }
    return;
  }
  const ok = await confirmPrompt("Print an AI-ready prompt to copy?", false);
  if (ok !== true) return;
  console.log("\n" + c.dim("──── copy the block below into any AI ────"));
  console.log(buildDiagnosisPrompt(action, root, res.exitCode, res.stderrTail));
  console.log(c.dim("──── end ────"));
}

class TerminalFrame {
  private active = false;
  private pending = "";
  private onData?: (buf: Buffer) => void;
  private readonly mouse: boolean;

  constructor(
    private readonly rawBefore: boolean,
    private readonly keyStream?: PassThrough,
    private readonly emitWheel?: (name: "wheelup" | "wheeldown") => void,
  ) {
    this.mouse = Boolean(keyStream);
  }

  enter(): void {
    if (this.active) return;
    // Alt screen + hidden cursor + clear. With mouse on, also enable SGR mouse tracking so the
    // wheel drives navigation (1000h = button events incl. wheel; 1006h = SGR extended coords).
    process.stdout.write("\x1b[?1049h\x1b[?25l\x1b[H\x1b[2J" + (this.mouse ? "\x1b[?1000h\x1b[?1006h" : ""));
    process.stdin.setRawMode(true);
    process.stdin.resume();
    if (this.keyStream) {
      // Only while the menu owns the terminal (not during enquirer prompts): filter raw stdin so
      // mouse bytes are consumed here and never reach readline; real key bytes flow to keyStream.
      this.pending = "";
      this.onData = (buf: Buffer) => this.filterInput(buf);
      process.stdin.on("data", this.onData);
    }
    this.active = true;
  }

  leave(): void {
    if (!this.active) return;
    if (this.onData) { process.stdin.off("data", this.onData); this.onData = undefined; }
    process.stdin.setRawMode(this.rawBefore);
    // Disable mouse tracking before leaving the alt screen so the host terminal is restored clean.
    process.stdout.write((this.mouse ? "\x1b[?1006l\x1b[?1000l" : "") + "\x1b[?25h\x1b[?1049l");
    this.active = false;
  }

  /** Consume mouse escape sequences (wheel → emitWheel; clicks/moves dropped), pass real keys on. */
  private filterInput(buf: Buffer): void {
    const s = this.pending + buf.toString("latin1");
    this.pending = "";
    let out = "";
    let i = 0;
    while (i < s.length) {
      const rest = s.slice(i);
      const sgr = /^\x1b\[<(\d+);\d+;\d+([Mm])/.exec(rest); // SGR 1006: ESC [ < btn ; col ; row M|m
      if (sgr) {
        if (sgr[2] === "M" && (sgr[1] === "64" || sgr[1] === "65")) this.emitWheel?.(sgr[1] === "64" ? "wheelup" : "wheeldown");
        i += sgr[0].length;
        continue;
      }
      if (/^\x1b\[<[\d;]*$/.test(rest)) { this.pending = rest; break; }      // incomplete SGR mouse — hold for next chunk
      if (rest.startsWith("\x1b[M")) {                                        // legacy X10 mouse: ESC [ M + 3 bytes
        if (rest.length >= 6) { i += 6; continue; }
        this.pending = rest;
        break;
      }
      out += s[i];
      i++;
    }
    if (out) this.keyStream?.write(Buffer.from(out, "latin1"));
  }
}
