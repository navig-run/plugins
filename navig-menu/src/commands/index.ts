import { writeFileSync, readFileSync, mkdirSync, existsSync } from "node:fs";
import { resolve, isAbsolute, join, basename } from "node:path";
import {
  getManifest,
  scanProject,
  writeCache,
  loadDefinition,
  buildMenuModel,
  type MenuModel,
} from "../builder/build.js";
import { definitionPath, navigDir } from "../manifest/paths.js";
import type { Manifest, Action, MenuDefinition } from "../manifest/schema.js";
import { MenuDefinitionSchema } from "../manifest/schema.js";
import { generateMenuDefinition, type GenerateAudit } from "../builder/generate.js";
import { importCatalog, NavigCatalogSchema, type ImportAudit } from "../builder/import-catalog.js";
import { createTheme } from "../ui/theme.js";
import { header, status, kv, hint, bullet } from "../ui/report.js";
import { resolveSettings, saveSettings, saveHidden, saveNote, savePluginState, type Settings } from "../ui/settings.js";
import { renderMenuSnapshot, runMenu } from "../ui/menu.js";
import { loadPlugins } from "../plugins/loader.js";
import { makeActionContext } from "../plugins/apply.js";
import { readJson } from "../detectors/fs.js";
import { checkReadiness } from "../detectors/readiness.js";
import { reviewBlock, confirmRisk } from "../ui/confirm.js";
import { confirmPrompt, CANCEL } from "../ui/prompts.js";
import { runActionLocal } from "../runners/local.js";
import { diagnose } from "../runners/diagnose.js";
import { aiDiagnose, buildDiagnosisPrompt, aiAvailable, aiEnrichMenu, aiSuggestScript, AI_UNSAFE_CANONICALS } from "../runners/ai.js";
import { emitActionRequest } from "../runners/relay.js";
import { gitInfo } from "../util/git.js";
import { detectNavig } from "../navig/detect.js";
import { TOOL_VERSION, TOOL_NAME } from "../config/constants.js";
import { MENU_GEN_PROMPT } from "./menu-gen-prompt.js";

export interface GlobalOpts {
  cwd: string;
  json: boolean;
  deep: boolean;
  plain: boolean;
  yes: boolean;
  relay: boolean;
  host?: string;
  ai: boolean;
  noCache: boolean;
  /** `import`/`organize`: persist the result to .navig/menu.json (else print a preview). */
  write: boolean;
  /** `doctor`: propose + apply fixes (confirm-gated, with a backup) instead of read-only. */
  fix: boolean;
}

const isTTY = () => Boolean(process.stdout.isTTY && process.stdin.isTTY);
const GENERATED_MENU_SCRIPT = "menu:navig";

/* ── menu (default) ────────────────────────────────────────────────────────── */

export async function cmdMenu(o: GlobalOpts): Promise<number> {
  const root = o.cwd;
  const manifest = getManifest(root, { deep: o.deep, noCache: o.noCache });
  const load = loadDefinition(root);

  if (o.json) {
    process.stdout.write(JSON.stringify(manifest, null, 2) + "\n");
    return 0;
  }

  // Load plugins once (async: dynamic import for programmatic local/npm), then reuse across rebuilds.
  const plugins = await loadPlugins(root, depsOf(root));
  let model = buildMenuModel(manifest, load, plugins);

  if (!isTTY() && !o.relay) {
    printSummary(model, manifest, o.plain);
    return 0;
  }

  let settings = resolveSettings(model.ui, model.accent);
  const theme = createTheme({
    plain: o.plain,
    accent: settings.accent,
    glyphs: settings.glyphs === "auto" ? undefined : settings.glyphs,
  });
  await offerSetupGuidance(root, manifest, theme, o);
  return runMenu(model, {
    root,
    theme,
    settings,
    plain: o.plain,
    mode: o.relay ? "relay" : "local",
    relay: o.relay,
    opts: { yes: o.yes },
    git: gitInfo(root),
    manifest,
    toolVersion: TOOL_VERSION,
    allActions: manifest.actions,
    hidden: loadDefinition(root).def?.hide ?? [],
    loaded: plugins,
    rebuild: () => {
      const fresh = scanProject(root, { deep: o.deep });
      writeCache(root, fresh);
      model = buildMenuModel(fresh, loadDefinition(root), plugins);
      return model;
    },
    regenerate: () => {
      const fresh = scanProject(root, { deep: o.deep });
      const { definition } = generateMenuDefinition(fresh, loadDefinition(root).def ?? {});
      mkdirSync(navigDir(root), { recursive: true });
      writeFileSync(definitionPath(root), JSON.stringify(definition, null, 2) + "\n", "utf8");
      writeCache(root, fresh);
      model = buildMenuModel(fresh, loadDefinition(root), plugins);
      settings = resolveSettings(model.ui, model.accent);
      return model;
    },
    organize: async () => {
      const fresh = scanProject(root, { deep: o.deep });
      const organized = await buildOrganizedDefinition(fresh, loadDefinition(root).def ?? {});
      if (!organized) return null;
      mkdirSync(navigDir(root), { recursive: true });
      writeFileSync(definitionPath(root), JSON.stringify(organized.definition, null, 2) + "\n", "utf8");
      writeCache(root, fresh);
      model = buildMenuModel(fresh, loadDefinition(root), plugins);
      settings = resolveSettings(model.ui, model.accent);
      return model;
    },
    persistSettings: (next: Settings) => {
      saveSettings(root, next, manifest);
      settings = next;
      model = buildMenuModel(getManifest(root, { deep: o.deep }), loadDefinition(root), plugins);
      return model;
    },
    persistHidden: (ids: string[]) => {
      saveHidden(root, ids, manifest);
      model = buildMenuModel(getManifest(root, { deep: o.deep }), loadDefinition(root), plugins);
      return model;
    },
    persistPluginState: (id, patch) => {
      savePluginState(root, id, patch, manifest);
      model = buildMenuModel(getManifest(root, { deep: o.deep }), loadDefinition(root), plugins);
      return model;
    },
    persistNote: (id, note) => {
      saveNote(root, id, note, manifest);
      model = buildMenuModel(getManifest(root, { deep: o.deep }), loadDefinition(root), plugins);
      return model;
    },
  });
}

/** Merged dependency names (for npm plugin discovery). */
function depsOf(root: string): Record<string, string> {
  const pkg = readJson<{
    dependencies?: Record<string, string>;
    devDependencies?: Record<string, string>;
    optionalDependencies?: Record<string, string>;
    peerDependencies?: Record<string, string>;
  }>(resolve(root, "package.json"));
  return {
    ...(pkg?.dependencies ?? {}),
    ...(pkg?.devDependencies ?? {}),
    ...(pkg?.optionalDependencies ?? {}),
    ...(pkg?.peerDependencies ?? {}),
  };
}

/* ── scan ──────────────────────────────────────────────────────────────────── */

export async function cmdScan(o: GlobalOpts): Promise<number> {
  const manifest = getManifest(o.cwd, { deep: o.deep, noCache: true });
  if (o.json) {
    process.stdout.write(JSON.stringify(manifest, null, 2) + "\n");
    return 0;
  }
  const model = buildMenuModel(manifest, loadDefinition(o.cwd));
  printSummary(model, manifest, o.plain);
  return 0;
}

/* ── build / generate (the menu definition) ─────────────────────────────────── */

export async function cmdBuild(o: GlobalOpts): Promise<number> {
  const root = o.cwd;
  const manifest = scanProject(root, { deep: o.deep });
  const theme = createTheme({ plain: o.plain });
  const { c, sym } = theme;

  if (o.ai) {
    // With a provider key: enrich the menu inline. Without one: keep the prompt-emission
    // fallback so an external AI (Claude Code / the NAVIG operator) can author it.
    if (aiAvailable()) return buildWithAi(root, manifest, o, theme);
    const navig = await detectNavig();
    if (navig.present) {
      console.log(c.dim("navig detected — relaying AI build to the operator (run `navig menu build --ai`)."));
    }
    console.log(c.dim("\n--- BEGIN MANIFEST ---"));
    console.log(JSON.stringify(manifest, null, 2));
    console.log(c.dim("--- END MANIFEST ---\n"));
    console.log(MENU_GEN_PROMPT);
    return 0;
  }

  // Pro generator: write/merge .navig/menu.json, preserving any human edits.
  const existing = loadDefinition(root).def ?? {};
  const generated = generateMenuDefinition(manifest, existing);
  mkdirSync(navigDir(root), { recursive: true });
  if (o.json) {
    process.stdout.write(JSON.stringify(generated.definition, null, 2) + "\n");
    return 0;
  }
  writeCache(root, manifest);
  writeFileSync(definitionPath(root), JSON.stringify(generated.definition, null, 2) + "\n", "utf8");
  console.log(`  ${c.green(sym.complete)} wrote ${c.cyan(".navig/menu.json")} (${Object.keys(generated.definition.actions ?? {}).length} canonical actions)`);
  await ensureMenuScript(root, theme, { assumeYes: true, pm: manifest.packageManager.value });
  printGenerateAudit(generated.audit, theme);
  console.log(`  ${c.dim("edit .navig/menu.json to customize labels, groups, risk, UI, and extra commands; regeneration preserves edits.")}`);
  return 0;
}

/**
 * `--ai build` with a provider key: the deterministic generator produces the factual baseline, then
 * an LLM curates DISPLAY fields (labels/descriptions/grouping + project purpose) via `overrides`.
 * Safety: AI never touches what runs (launcher/argv/cwd) or risk tiers, and human `overrides` win
 * the merge. Falls back to the deterministic menu if the call fails.
 */
async function buildWithAi(
  root: string,
  manifest: Manifest,
  o: GlobalOpts,
  theme: ReturnType<typeof createTheme>,
): Promise<number> {
  const { c, sym } = theme;
  const existing = loadDefinition(root).def ?? {};
  const generated = generateMenuDefinition(manifest, existing);
  const def = generated.definition;

  // Dedup detected scripts by id for the curation request.
  const seen = new Set<string>();
  const scripts: { id: string; cmd: string; group?: string }[] = [];
  for (const a of manifest.actions) {
    if (seen.has(a.id)) continue;
    seen.add(a.id);
    scripts.push({ id: a.id, cmd: `${a.launcher} ${a.argv.join(" ")}`.trim(), group: a.group });
  }

  if (!o.json) console.log("  " + c.dim("curating the menu with AI…"));
  const enrich = await aiEnrichMenu({
    name: def.title ?? manifest.name ?? "project",
    stack: manifest.frameworks.map((f) => f.id),
    scripts,
  });

  let curated = 0;
  if (enrich) {
    if (enrich.purpose && !existing.purpose) def.purpose = enrich.purpose;
    if (enrich.overrides) {
      // AI fills the base; human `overrides` win per-field (edits are never clobbered).
      const merged: Record<string, Record<string, unknown>> = { ...(enrich.overrides as any) };
      for (const [id, h] of Object.entries(existing.overrides ?? {})) {
        merged[id] = { ...(merged[id] ?? {}), ...(h as Record<string, unknown>) };
      }
      def.overrides = merged as NonNullable<typeof def.overrides>;
      curated = Object.keys(enrich.overrides).length;
    }
  }

  const finalDef = MenuDefinitionSchema.parse(def); // validate the merged result
  mkdirSync(navigDir(root), { recursive: true });
  if (o.json) {
    process.stdout.write(JSON.stringify(finalDef, null, 2) + "\n");
    return 0;
  }
  writeCache(root, manifest);
  writeFileSync(definitionPath(root), JSON.stringify(finalDef, null, 2) + "\n", "utf8");
  if (enrich) {
    console.log(
      `  ${c.green(sym.complete)} wrote ${c.cyan(".navig/menu.json")} — AI-curated ${curated} script${curated === 1 ? "" : "s"}${enrich.purpose ? " + purpose" : ""}`,
    );
  } else {
    console.log(`  ${c.yellow(sym.warning)} AI unavailable — wrote the deterministic menu instead.`);
  }
  await ensureMenuScript(root, theme, { assumeYes: true, pm: manifest.packageManager.value });
  printGenerateAudit(generated.audit, theme);
  console.log(`  ${c.dim("edit .navig/menu.json to customize; regeneration preserves your edits.")}`);
  return 0;
}

/**
 * Before opening the menu, flag a project that isn't set up yet (deps not installed, missing `.env`,
 * no virtualenv) and offer to install dependencies. Deterministic; skipped with `--yes`.
 */
async function offerSetupGuidance(
  root: string,
  manifest: Manifest,
  theme: ReturnType<typeof createTheme>,
  o: GlobalOpts,
): Promise<void> {
  if (o.yes) return;
  const issues = checkReadiness(root, manifest);
  if (issues.length === 0) return;
  const { c, sym } = theme;
  console.log("\n  " + c.yellow(`${sym.warning} This project may not be set up yet:`));
  for (const i of issues) console.log("    " + c.dim("• " + i.message));

  const withFix = issues.find((i) => i.fix);
  if (withFix?.fix) {
    const cmd = `${withFix.fix.launcher} ${withFix.fix.argv.join(" ")}`.trim();
    const ok = await confirmPrompt(`Run ${c.cyan(cmd)} now?`, true);
    if (ok === true) {
      const fix: Action = {
        id: "setup:install",
        label: withFix.fix.label,
        group: "Setup",
        launcher: withFix.fix.launcher,
        argv: withFix.fix.argv,
        cwd: ".",
        risk: "safe",
        longRunning: false,
        confidence: "detected",
        evidence: [],
        source: "custom",
      };
      console.log("");
      await runActionLocal(fix, root);
    }
  }
  console.log("");
}

/* ── list / preview ────────────────────────────────────────────────────────── */

export async function cmdList(o: GlobalOpts): Promise<number> {
  const root = o.cwd;
  const manifest = getManifest(root, { deep: o.deep, noCache: o.noCache });
  const plugins = await loadPlugins(root, depsOf(root));
  const model = buildMenuModel(manifest, loadDefinition(root), plugins);
  if (o.json) {
    process.stdout.write(JSON.stringify(menuListJson(model), null, 2) + "\n");
    return 0;
  }
  const settings = resolveSettings(model.ui, model.accent);
  const theme = createTheme({
    plain: o.plain,
    accent: settings.accent,
    glyphs: settings.glyphs === "auto" ? undefined : settings.glyphs,
  });
  process.stdout.write(
    renderMenuSnapshot(model, {
      root,
      theme,
      mode: o.relay ? "relay" : "local",
      git: gitInfo(root),
      manifest,
      settings,
      toolVersion: TOOL_VERSION,
    }, { height: 36 }) + "\n",
  );
  return 0;
}

export const cmdGenerate = cmdBuild;

function printGenerateAudit(audit: GenerateAudit, theme: ReturnType<typeof createTheme>): void {
  const line = (label: string, values: string[], fallback: string) =>
    console.log(kv(theme, label, values.length ? values.join(", ") : fallback));
  console.log(status(theme, "info", "generator audit"));
  line("mapped", audit.mapped, "none");
  line("preserved", audit.preserved, "none");
  line("hidden", audit.hidden, "none");
  line("ui", audit.ui, "defaults");
  if (audit.extras) console.log(kv(theme, "extras", String(audit.extras)));
}

/* ── run / dev / test ────────────────────────────────────────────────────────── */

export async function cmdRun(o: GlobalOpts, target: string): Promise<number> {
  const root = o.cwd;
  const manifest = getManifest(root, { deep: o.deep, noCache: o.noCache });
  // Full async plugin load so plugin-contributed actions (incl. programmatic local .mjs plugins)
  // are runnable headlessly, not only from the interactive menu.
  const plugins = await loadPlugins(root, depsOf(root));
  const model = buildMenuModel(manifest, loadDefinition(root), plugins);
  const action =
    model.allScripts.find((a) => a.canonical === target) ??
    model.allScripts.find((a) => a.id === target);
  if (!action) {
    process.stderr.write(`No action "${target}". Try: navig-menu --json | (open) navig-menu\n`);
    return 2;
  }
  const theme = createTheme({ plain: o.plain });
  console.log(reviewBlock(action, root, theme));
  const ok = await confirmRisk(action, theme, { yes: o.yes }, root);
  if (!ok) return 1;

  // Plugin internal actions dispatch to their handler in-process (there is no spawnable argv).
  // Unlike the menu's isolated dispatch, surface honest exit codes for CI use.
  if (action.source.startsWith("plugin:internal:")) {
    const [, , pluginId = "", handlerKey = ""] = action.source.split(":");
    const handler = plugins.find((l) => l.plugin.id === pluginId)?.plugin.handlers?.[handlerKey];
    if (!handler) {
      process.stderr.write(`No handler "${handlerKey}" in plugin "${pluginId}".\n`);
      return 2;
    }
    const settingsFor: Record<string, boolean | string> = {};
    for (const s of model.pluginSettings ?? []) if (s.pluginId === pluginId) settingsFor[s.key] = s.value;
    let msg = "";
    const actx = makeActionContext({
      root,
      manifest,
      pluginId,
      settings: settingsFor,
      theme,
      notify: (m) => {
        msg = m;
      },
    });
    try {
      await handler(actx);
    } catch (e) {
      process.stderr.write(`plugin ${pluginId} failed: ${(e as Error).message}\n`);
      return 1;
    }
    if (msg) console.log("  " + theme.c.green(msg));
    return 0;
  }

  if (o.relay) {
    emitActionRequest(action, root);
    return 0;
  }
  const res = await runActionLocal(action, root);
  if (res.exitCode !== 0) {
    const dx = diagnose(action, root, res.exitCode, res.stderrTail);
    if (dx) {
      process.stderr.write(`\n  ${dx.title}\n  ${dx.hint}\n`);
      if (dx.fix) {
        process.stderr.write(`  fix: ${dx.fix.launcher} ${dx.fix.argv.join(" ")}  (in ${dx.fix.cwd})\n`);
      }
    } else if (o.ai && res.stderrTail.trim()) {
      // Unknown failure + explicit --ai → try an LLM, else emit a paste-ready prompt.
      const ai = await aiDiagnose(action, root, res.exitCode, res.stderrTail);
      if (ai) {
        process.stderr.write(`\n  ${ai.title} (AI)\n  ${ai.hint}\n`);
        if (ai.command) process.stderr.write(`  suggested: ${ai.command}\n`);
      } else {
        process.stderr.write(`\n${buildDiagnosisPrompt(action, root, res.exitCode, res.stderrTail)}\n`);
      }
    }
  }
  return res.exitCode;
}

/* ── doctor ──────────────────────────────────────────────────────────────────── */

export async function cmdDoctor(o: GlobalOpts): Promise<number> {
  const theme = createTheme({ plain: o.plain });
  const { c } = theme;
  const manifest = getManifest(o.cwd, { noCache: true });
  const navig = await detectNavig();
  const def = loadDefinition(o.cwd);
  const plugins = await loadPlugins(o.cwd, depsOf(o.cwd));
  const model = buildMenuModel(manifest, def, plugins);
  const hasDef = existsSync(definitionPath(o.cwd));

  console.log(header(theme, "navig-menu doctor"));
  console.log(status(theme, "ok", `${TOOL_NAME} v${TOOL_VERSION}`));
  console.log(status(theme, "ok", `node ${process.version} · ${process.platform}/${process.arch}`));
  console.log(status(theme, "ok", `cwd ${o.cwd}`));
  console.log(
    status(theme, manifest.packageManager.value !== "none" ? "ok" : "warn",
      `package manager: ${manifest.packageManager.value} ${c.dim(`(${manifest.packageManager.confidence})`)}`),
  );
  console.log(
    status(theme, navig.present ? "ok" : "muted",
      `navig: ${navig.present ? (navig.gateway ? "gateway live" : "on PATH") : "not detected"}`),
  );
  console.log(
    status(theme, def.error ? "fail" : hasDef ? "ok" : "muted",
      `.navig/menu.json: ${def.error ? c.red(def.error) : hasDef ? "valid" : "none (run `navig-menu build`)"}`),
  );

  const loaded = model.loadedPlugins ?? [];
  const active = loaded.filter((p) => p.active);
  console.log(status(theme, "ok", `plugins: ${loaded.length} discovered · ${active.length} active`));
  for (const p of loaded) {
    const state = p.error ? c.red("error") : p.active ? c.green(p.auto ? "on (auto)" : "on") : c.dim("off");
    const detail = `${p.id} — ${p.tier}/${p.origin}${p.version ? ` v${p.version}` : ""} · ${state}`;
    console.log(p.error ? status(theme, "warn", `${detail}${c.dim(` (${p.error})`)}`, 1) : bullet(theme, detail, 1));
  }

  const readiness = checkReadiness(o.cwd, manifest);
  if (readiness.length === 0) {
    console.log(status(theme, "ok", "setup: ready"));
  } else {
    for (const r of readiness) console.log(status(theme, "warn", `setup: ${r.message}`));
  }

  for (const w of manifest.warnings) console.log(status(theme, "warn", `${w.code}: ${w.detail}`));

  if (o.fix) return runDoctorFixes(o, manifest, def.def ?? {}, theme);
  return 0;
}

/**
 * `doctor --fix`: propose + apply repairs, tiered by confidence. High-confidence fixes (a missing
 * `menu` script, orphaned overrides, and — with `--ai` — a body for a missing canonical) are applied
 * confirm-gated with a package.json backup. Broken `cd` paths are advisory only (a mis-detected path
 * must never be auto-rewritten). `--yes` batches the confirmations.
 */
async function runDoctorFixes(
  o: GlobalOpts,
  manifest: Manifest,
  existing: MenuDefinition,
  theme: ReturnType<typeof createTheme>,
): Promise<number> {
  const root = o.cwd;
  const { c } = theme;
  const { audit } = generateMenuDefinition(manifest, existing);
  const broken = brokenScriptPaths(root, manifest.scripts);

  const pkgPath = resolve(root, "package.json");
  const pkg = readPkgScripts(pkgPath);
  const pkgInvalid = existsSync(pkgPath) && !pkg;
  const missingMenuScript = !!pkg && !pkg.scripts?.menu && !pkg.scripts?.["menu:navig"];

  console.log("");
  console.log(header(theme, "doctor · fix"));
  if (pkgInvalid) {
    console.log(status(theme, "warn", "package.json is not valid JSON — fix it, then re-run for package.json repairs."));
  }

  // Advisory: a stale `cd <dir>` path. Never auto-rewritten (a wrong guess would break a good script).
  for (const b of broken) {
    console.log(status(theme, "warn", `${c.cyan(b.id)} runs \`cd ${b.dir}\` but that directory is missing ${c.dim("(advisory — fix by hand)")}`));
  }

  // Collect apply-able package.json script additions (only if package.json is parseable).
  const scriptPatch: Record<string, string> = {};
  if (!pkgInvalid) {
    if (missingMenuScript) {
      const cmd = menuScriptCommand(manifest.packageManager.value);
      scriptPatch.menu = cmd;
      scriptPatch[GENERATED_MENU_SCRIPT] = cmd;
    }
    if (o.ai && audit.missing.length) {
      const samples = Object.entries(manifest.scripts).slice(0, 12).map(([k, v]) => `${k}: ${v}`);
      const stack = manifest.frameworks.map((f) => f.id);
      for (const canonical of audit.missing) {
        if (AI_UNSAFE_CANONICALS.has(canonical)) {
          console.log(hint(theme, `skipping "${canonical}" — AI won't author a destructive canonical; add it by hand.`));
          continue;
        }
        const body = await aiSuggestScript(canonical, stack, samples);
        if (body) scriptPatch[canonical] = body;
      }
    } else if (audit.missing.length) {
      console.log(status(theme, "muted", `no ${c.cyan(audit.missing.join(", "))} script`));
      console.log(hint(theme, "these are optional standard actions — add with `doctor --fix --ai`, or ignore.", 1));
    }
  }

  const prunable = audit.orphans;
  if (Object.keys(scriptPatch).length === 0 && prunable.length === 0) {
    if (broken.length === 0) console.log(status(theme, "ok", "everything looks healthy — nothing to fix."));
    return 0;
  }

  // Apply package.json script additions (confirm-gated, with a backup).
  if (Object.keys(scriptPatch).length) {
    for (const [name, cmd] of Object.entries(scriptPatch)) console.log(status(theme, "add", `${c.cyan(`"${name}"`)}: ${cmd}`));
    const ok = o.yes || (await confirmPrompt(`Apply ${Object.keys(scriptPatch).length} package.json script change(s)?`, true)) === true;
    if (ok) {
      const res = editPackageScripts(root, scriptPatch);
      if (res.error) {
        console.log(status(theme, "warn", `${res.error} — no changes written.`));
      } else if (res.changed.length) {
        console.log(status(theme, "ok", `updated ${c.cyan("package.json")} (${res.changed.join(", ")})`));
        if (res.backupPath) console.log(hint(theme, `backup: ${res.backupPath}`, 1));
      } else {
        console.log(hint(theme, "package.json already up to date."));
      }
    } else {
      console.log(hint(theme, "skipped package.json changes."));
    }
  }

  // Prune orphaned overrides from .navig/menu.json.
  if (prunable.length) {
    console.log(status(theme, "remove", `prune ${prunable.length} orphaned override(s): ${prunable.join(", ")}`));
    const ok = o.yes || (await confirmPrompt(`Remove ${prunable.length} orphaned override(s) from .navig/menu.json?`, true)) === true;
    if (ok) {
      const overrides = { ...(existing.overrides ?? {}) };
      for (const id of prunable) delete overrides[id];
      const cleaned = MenuDefinitionSchema.parse({
        ...existing,
        overrides: Object.keys(overrides).length ? overrides : undefined,
      });
      mkdirSync(navigDir(root), { recursive: true });
      const backup = backupFile(root, definitionPath(root));
      writeFileSync(definitionPath(root), JSON.stringify(cleaned, null, 2) + "\n", "utf8");
      console.log(status(theme, "ok", `pruned orphaned overrides → ${c.cyan(".navig/menu.json")}`));
      if (backup) console.log(hint(theme, `backup: ${backup}`, 1));
    } else {
      console.log(hint(theme, "kept orphaned overrides."));
    }
  }

  return 0;
}

/* ── import (curated catalog → .navig/menu.json) ─────────────────────────────── */

const DEFAULT_CATALOG = "scripts/menu.catalog.json";

/**
 * Import a hand-curated command catalog (default `scripts/menu.catalog.json`) into the menu
 * definition — exact labels/descriptions with zero AI. Merge-preserving: human `overrides` win and
 * hand-authored `extra`/`groups` survive, so re-running is safe. Prints a preview + audit; only
 * writes with `--write`.
 */
export async function cmdImport(o: GlobalOpts, target?: string): Promise<number> {
  const root = o.cwd;
  const theme = createTheme({ plain: o.plain });
  const { c } = theme;

  const rel = target ?? DEFAULT_CATALOG;
  const abs = isAbsolute(rel) ? rel : resolve(root, rel);
  if (!existsSync(abs)) {
    process.stderr.write(status(theme, "fail", `catalog not found: ${rel}`) + "\n" + hint(theme, "pass a path: navig-menu import <catalog.json>") + "\n");
    return 2;
  }

  let catalog;
  try {
    catalog = NavigCatalogSchema.parse(JSON.parse(readFileSync(abs, "utf8")));
  } catch (e) {
    process.stderr.write(status(theme, "fail", `${rel} is not a valid catalog: ${(e as Error).message}`) + "\n");
    return 2;
  }

  const manifest = scanProject(root, { deep: o.deep });
  const existing = loadDefinition(root).def ?? {};
  const { definition, audit } = importCatalog(catalog, existing, manifest);

  if (o.json) {
    process.stdout.write(JSON.stringify(definition, null, 2) + "\n");
    return 0;
  }

  console.log(header(theme, "navig-menu import"));
  console.log(hint(theme, `${theme.sym.back} ${c.cyan(rel)}`));
  printImportAudit(audit, theme);

  if (!o.write) {
    console.log(hint(theme, `preview only — re-run with ${c.cyan("--write")} to update .navig/menu.json`));
    return 0;
  }

  mkdirSync(navigDir(root), { recursive: true });
  writeCache(root, manifest);
  writeFileSync(definitionPath(root), JSON.stringify(definition, null, 2) + "\n", "utf8");
  console.log(status(theme, "ok", `wrote ${c.cyan(".navig/menu.json")} — ${audit.imported.length} commands across ${audit.groups.length} sections`));
  console.log(hint(theme, "curate any imported command via `overrides` in .navig/menu.json — your edits win on re-import."));
  return 0;
}

/* ── organize (AI: describe every command + report gaps) ─────────────────────── */

export interface OrganizeResult {
  definition: MenuDefinition;
  described: number;
  purpose: boolean;
}

/**
 * Describe every still-undescribed detected script with AI and merge the results into `overrides`
 * (human edits win per field). Returns null when there's nothing to do or no AI is available.
 * Pure of I/O — callers decide whether to persist. Shared by `cmdOrganize` and the in-menu rail.
 */
export async function buildOrganizedDefinition(
  manifest: Manifest,
  existing: MenuDefinition,
): Promise<OrganizeResult | null> {
  const hidden = new Set(existing.hide ?? []);
  const described = new Set(
    Object.entries(existing.overrides ?? {})
      .filter(([, v]) => v.description)
      .map(([id]) => id),
  );
  const seen = new Set<string>();
  const targets: { id: string; cmd: string; group?: string }[] = [];
  for (const a of manifest.actions) {
    if (seen.has(a.id) || hidden.has(a.id) || described.has(a.id)) continue;
    seen.add(a.id);
    targets.push({ id: a.id, cmd: `${a.launcher} ${a.argv.join(" ")}`.trim(), group: a.group });
  }
  if (targets.length === 0 || !aiAvailable()) return null;

  const enrich = await aiEnrichMenu({
    name: existing.title ?? manifest.name ?? "project",
    stack: manifest.frameworks.map((f) => f.id),
    scripts: targets,
  });
  if (!enrich?.overrides) return null;

  // AI fills the base; existing (human) overrides win per field so edits are never clobbered.
  const merged: Record<string, Record<string, unknown>> = { ...(enrich.overrides as any) };
  for (const [id, h] of Object.entries(existing.overrides ?? {})) {
    merged[id] = { ...(merged[id] ?? {}), ...(h as Record<string, unknown>) };
  }
  const patch: MenuDefinition = { ...existing, overrides: merged as NonNullable<MenuDefinition["overrides"]> };
  if (enrich.purpose && !existing.purpose) patch.purpose = enrich.purpose;
  const definition = MenuDefinitionSchema.parse(patch);
  return { definition, described: Object.keys(enrich.overrides).length, purpose: Boolean(enrich.purpose && !existing.purpose) };
}

/**
 * `navig-menu organize` — report gaps (missing canonical actions, orphaned overrides), then use AI
 * to write a description for every command the menu can't yet explain. Merge-preserving; only writes
 * with `--write`. No key → emit a paste-ready authoring prompt for any external AI.
 */
export async function cmdOrganize(o: GlobalOpts): Promise<number> {
  const root = o.cwd;
  const theme = createTheme({ plain: o.plain });
  const { c } = theme;
  const manifest = scanProject(root, { deep: o.deep });
  const existing = loadDefinition(root).def ?? {};
  const { audit } = generateMenuDefinition(manifest, existing);

  console.log(header(theme, "navig-menu organize"));
  if (audit.missing.length) {
    console.log(status(theme, "warn", `missing canonical actions: ${c.cyan(audit.missing.join(", "))}  ${c.dim("(add with `doctor --fix`)")}`));
  }
  if (audit.orphans.length) {
    console.log(status(theme, "warn", `orphaned overrides (target no known command): ${audit.orphans.join(", ")}`));
  }
  if (!audit.missing.length && !audit.orphans.length) {
    console.log(status(theme, "ok", "no missing canonical actions, no orphaned overrides"));
  }

  // Count what still needs describing (for the no-key / nothing-to-do messages).
  const hidden = new Set(existing.hide ?? []);
  const describedIds = new Set(Object.entries(existing.overrides ?? {}).filter(([, v]) => v.description).map(([id]) => id));
  const needing = new Set(manifest.actions.filter((a) => !hidden.has(a.id) && !describedIds.has(a.id)).map((a) => a.id));

  if (needing.size === 0) {
    console.log(status(theme, "ok", "every command already has a description — nothing to organize."));
    return 0;
  }

  if (!aiAvailable()) {
    console.log(status(theme, "info", `${needing.size} command(s) need a description — no AI key.`));
    console.log(hint(theme, "paste the block below into any AI to author them:", 1));
    console.log(c.dim("\n--- BEGIN MANIFEST ---"));
    console.log(JSON.stringify(manifest, null, 2));
    console.log(c.dim("--- END MANIFEST ---\n"));
    console.log(MENU_GEN_PROMPT);
    return 0;
  }

  console.log(hint(theme, `describing ${needing.size} command(s) with AI…`));
  const organized = await buildOrganizedDefinition(manifest, existing);
  if (!organized) {
    console.log(status(theme, "warn", "AI unavailable or returned nothing — no changes."));
    return 1;
  }

  if (o.json) {
    process.stdout.write(JSON.stringify(organized.definition, null, 2) + "\n");
    return 0;
  }
  if (!o.write) {
    console.log(status(theme, "info", `would describe ${organized.described} command(s)${organized.purpose ? " + set the project purpose" : ""} — re-run with ${c.cyan("--write")} to apply.`));
    return 0;
  }
  mkdirSync(navigDir(root), { recursive: true });
  writeCache(root, manifest);
  writeFileSync(definitionPath(root), JSON.stringify(organized.definition, null, 2) + "\n", "utf8");
  console.log(status(theme, "ok", `organized — described ${organized.described} command(s) → ${c.cyan(".navig/menu.json")}`));
  return 0;
}

function printImportAudit(audit: ImportAudit, theme: ReturnType<typeof createTheme>): void {
  console.log(status(theme, "info", "import audit"));
  console.log(kv(theme, "imported", `${audit.imported.length} commands`));
  console.log(kv(theme, "sections", audit.groups.length ? audit.groups.join(", ") : "none"));
  console.log(kv(theme, "demoted", audit.demoted.length ? `${audit.demoted.length} duplicate root scripts hidden` : "none"));
  const skipped = audit.skippedSpecial.length + audit.skippedShell.length + audit.skippedEmpty.length;
  if (skipped) {
    console.log(status(theme, "warn", `${skipped} item${skipped === 1 ? "" : "s"} not imported (stay in the source menu):`));
    if (audit.skippedSpecial.length) console.log(kv(theme, "interactive", audit.skippedSpecial.join(", "), { depth: 1 }));
    if (audit.skippedShell.length) console.log(kv(theme, "shell-op", audit.skippedShell.join(", "), { depth: 1 }));
    if (audit.skippedEmpty.length) console.log(kv(theme, "no command", audit.skippedEmpty.join(", "), { depth: 1 }));
  }
}

/* ── setup ───────────────────────────────────────────────────────────────────── */

export async function cmdSetup(o: GlobalOpts): Promise<number> {
  const root = o.cwd;
  const theme = createTheme({ plain: o.plain });
  const { c, sym } = theme;

  // 1. Ensure a definition exists.
  if (!existsSync(definitionPath(root))) {
    await cmdBuild({ ...o, ai: false });
  } else {
    console.log(`  ${c.dim(".navig/menu.json already exists — keeping it.")}`);
  }

  // 2. Offer the consented `menu` script (the only package.json edit we ever make).
  const pm = getManifest(root).packageManager.value;
  await ensureMenuScript(root, theme, { assumeYes: o.yes, prompt: true, pm });
  return 0;
}

interface EnsureMenuScriptOptions {
  assumeYes?: boolean;
  prompt?: boolean;
  /** Detected package manager, so the generated fallback uses `pnpm dlx` / `bunx` / `yarn dlx` / npx. */
  pm?: string;
}

export async function ensureMenuScript(
  root: string,
  theme: ReturnType<typeof createTheme>,
  opts: EnsureMenuScriptOptions = {},
): Promise<"added" | "alias-added" | "exists" | "skipped" | "missing-package"> {
  const { c, sym } = theme;
  const pkgPath = resolve(root, "package.json");
  if (!existsSync(pkgPath)) return "missing-package";
  const menuScript = menuScriptCommand(opts.pm);

  const pkg = readPkgScripts(pkgPath);
  if (!pkg) return "missing-package"; // malformed → can't safely edit; treat like absent
  const scripts = pkg.scripts ?? {};
  // "Ours" = any generated form (npx/pnpm dlx/yarn dlx/bunx) OR the exact command we'd write now
  // (covers the dynamic `node <path>` / NAVIG_MENU_SCRIPT forms). A hand-written value is never here.
  const generatedValues = new Set([...GENERATED_MENU_VALUES, menuScript]);
  if (scripts.menu && generatedValues.has(scripts.menu)) {
    if (scripts.menu === menuScript && scripts[GENERATED_MENU_SCRIPT] === menuScript) {
      console.log(`  ${c.dim('"menu" script already present.')}`);
      return "exists";
    }
    pkg.scripts = { ...scripts, menu: menuScript, [GENERATED_MENU_SCRIPT]: menuScript };
    writeFileSync(pkgPath, JSON.stringify(pkg, null, 2) + "\n", "utf8");
    console.log(`  ${c.green(sym.complete)} updated generated menu scripts — run \`npm run ${GENERATED_MENU_SCRIPT}\``);
    return "added";
  }

  if (scripts.menu) {
    if (scripts[GENERATED_MENU_SCRIPT] === menuScript) {
      console.log(`  ${c.dim(`"${GENERATED_MENU_SCRIPT}" script already present; existing "menu" script kept.`)}`);
      return "exists";
    }
    pkg.scripts = { ...scripts, [GENERATED_MENU_SCRIPT]: menuScript };
    writeFileSync(pkgPath, JSON.stringify(pkg, null, 2) + "\n", "utf8");
    console.log(`  ${c.green(sym.complete)} existing "menu" script kept; added "${GENERATED_MENU_SCRIPT}" for generated menu`);
    return "alias-added";
  }

  const yes = opts.assumeYes
      ? true
    : opts.prompt
      ? await confirmPrompt(`Add a "menu": "${menuScript}" script so \`npm run menu\` / \`pnpm menu\` open this? `, true)
      : true;
  if (yes !== true) return "skipped";

  pkg.scripts = { ...(pkg.scripts ?? {}), menu: menuScript, [GENERATED_MENU_SCRIPT]: menuScript };
  writeFileSync(pkgPath, JSON.stringify(pkg, null, 2) + "\n", "utf8");
  console.log(`  ${c.green(sym.complete)} added "menu" and "${GENERATED_MENU_SCRIPT}" scripts — run \`npm run menu\``);
  return "added";
}

/** The registry exec-runner for a package manager (fetch-and-run `navig-menu` without a local install). */
export function pmExecRunner(pm: string): string {
  switch (pm) {
    case "pnpm":
      return "pnpm dlx navig-menu";
    case "yarn":
      return "yarn dlx navig-menu";
    case "bun":
      return "bunx navig-menu";
    default:
      // npm / none / unknown — `--yes` so `npm run menu` never hangs on npx's "Ok to proceed?" prompt.
      return "npx --yes navig-menu";
  }
}

/**
 * Every value the tool itself can emit for the `menu` script. Used to tell "our" value (safe to
 * refresh) from a hand-written one (never touch), independent of which package manager wrote it —
 * so switching pm never makes us mistake our own script for the user's.
 */
const GENERATED_MENU_VALUES = new Set([
  "npx --yes navig-menu",
  "npx navig-menu",
  "navig-menu",
  "pnpm dlx navig-menu",
  "yarn dlx navig-menu",
  "bunx navig-menu",
]);

function menuScriptCommand(pm = "npm"): string {
  if (process.env.NAVIG_MENU_SCRIPT?.trim()) return process.env.NAVIG_MENU_SCRIPT.trim();
  const cliPath = process.argv[1] ? resolve(process.argv[1]) : undefined;
  const normalized = cliPath?.split("\\").join("/");
  if (cliPath && normalized && existsSync(cliPath) && !normalized.includes("/node_modules/")) {
    return `node ${quoteScriptPath(cliPath)}`;
  }
  return pmExecRunner(pm);
}

function quoteScriptPath(path: string): string {
  const normalized = path.split("\\").join("/");
  return /[\s()]/.test(normalized) ? `"${normalized.replace(/"/g, '\\"')}"` : normalized;
}

/* ── package.json script editing (the doctor's only repo mutation) ───────────── */

export interface EditScriptsResult {
  changed: string[];
  backupPath?: string;
  /** Set when package.json couldn't be parsed — nothing was written. */
  error?: string;
}

/** Read a package.json, tolerating a missing or malformed file (returns null instead of throwing). */
function readPkgScripts(pkgPath: string): { scripts?: Record<string, string> } | null {
  if (!existsSync(pkgPath)) return null;
  try {
    return JSON.parse(readFileSync(pkgPath, "utf8")) as { scripts?: Record<string, string> };
  } catch {
    return null;
  }
}

/** Snapshot an existing file to `.navig/backups/<name>.<stamp>.bak` before overwriting it. */
function backupFile(root: string, filePath: string): string | undefined {
  if (!existsSync(filePath)) return undefined;
  const dir = join(navigDir(root), "backups");
  mkdirSync(dir, { recursive: true });
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const dest = join(dir, `${basename(filePath)}.${stamp}.bak`);
  writeFileSync(dest, readFileSync(filePath, "utf8"), "utf8");
  return dest;
}

/**
 * Add/update `scripts` entries in package.json, writing a timestamped backup to `.navig/backups/`
 * first. Touches ONLY the `scripts` block; other keys and their order are preserved. Idempotent — a
 * value already in place is a no-op (and writes no backup). The single seam the doctor uses to
 * mutate a repo, so the backup + scripts-only guarantees live in one place.
 */
export function editPackageScripts(
  root: string,
  patch: Record<string, string>,
  opts: { backup?: boolean } = {},
): EditScriptsResult {
  const pkgPath = resolve(root, "package.json");
  if (!existsSync(pkgPath)) return { changed: [] };
  const raw = readFileSync(pkgPath, "utf8");
  let pkg: { scripts?: Record<string, string> };
  try {
    pkg = JSON.parse(raw) as { scripts?: Record<string, string> };
  } catch {
    return { changed: [], error: "package.json is not valid JSON" };
  }
  const scripts = { ...(pkg.scripts ?? {}) };
  const changed: string[] = [];
  for (const [name, cmd] of Object.entries(patch)) {
    if (scripts[name] !== cmd) {
      scripts[name] = cmd;
      changed.push(name);
    }
  }
  if (changed.length === 0) return { changed: [] };

  let backupPath: string | undefined;
  if (opts.backup !== false) {
    const dir = join(navigDir(root), "backups");
    mkdirSync(dir, { recursive: true });
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    backupPath = join(dir, `package.json.${stamp}.bak`);
    writeFileSync(backupPath, raw, "utf8");
  }
  pkg.scripts = scripts;
  writeFileSync(pkgPath, JSON.stringify(pkg, null, 2) + "\n", "utf8");
  return { changed, backupPath };
}

/** Detected scripts whose leading `cd <dir>` points at a directory that doesn't exist. Advisory. */
export function brokenScriptPaths(root: string, scripts: Record<string, string>): { id: string; dir: string }[] {
  const out: { id: string; dir: string }[] = [];
  for (const [id, raw] of Object.entries(scripts)) {
    const m = /^\s*cd\s+("[^"]+"|'[^']+'|\S+)\s*(?:&&|;)/.exec(raw);
    if (!m) continue;
    const dir = m[1]!.replace(/^["']|["']$/g, "");
    if (/[*?$%]/.test(dir)) continue; // skip globs / variable expansions — can't verify statically
    if (!existsSync(resolve(root, dir))) out.push({ id, dir });
  }
  return out;
}

/* ── shared human summary (non-TTY / scan) ───────────────────────────────────── */

function printSummary(model: MenuModel, manifest: Manifest, plain: boolean): void {
  const theme = createTheme({ plain, accent: model.accent });
  const { c, accent } = theme;
  console.log(header(theme, model.title));
  if (model.purpose) console.log(hint(theme, model.purpose));
  console.log(
    `  ${model.packageManager} ${c.dim(`(${model.pmConfidence})`)} · workspace: ${model.workspaceKind} · ${model.stats.scripts} scripts`,
  );
  if (model.frameworks.length) console.log(`  frameworks: ${model.frameworks.join(", ")}`);
  if (model.services.length) console.log(`  services: ${model.services.map((s) => `${s.id} (${s.confidence})`).join(", ")}`);
  if (model.endpoints.length)
    console.log(`  endpoints: ${model.endpoints.map((e) => `${e.label} → ${e.url}${e.tls ? " (TLS)" : ""}`).join(", ")}`);
  for (const g of model.groups) {
    console.log("");
    console.log(`  ${accent(g.group)}`);
    for (const a of g.items) {
      const tag = a.risk !== "safe" ? c.dim(` [${a.risk}]`) : "";
      console.log(bullet(theme, `${a.id}${tag}`, 1));
    }
  }
  for (const w of manifest.warnings) console.log(status(theme, "warn", `${w.code}: ${w.detail}`));
}

function menuListJson(model: MenuModel): {
  title: string;
  purpose?: string;
  groups: Array<{
    group: string;
    items: Array<{ id: string; label: string; command: string; risk: string; description?: string }>;
  }>;
} {
  return {
    title: model.title,
    purpose: model.purpose,
    groups: model.groups.map((group) => ({
      group: group.group,
      items: group.items.map((action) => ({
        id: action.id,
        label: action.label,
        command: `${action.launcher} ${action.argv.join(" ")}`.trim(),
        risk: action.risk,
        description: action.description,
      })),
    })),
  };
}
