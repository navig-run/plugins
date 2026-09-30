/**
 * Handlers for the Microsoft Store plugin — the only place with side effects (network, prompts,
 * file reads, cache writes). All Partner Center work delegates to the pure `engine/` modules.
 *
 * SECRET SAFETY: credentials are resolved at call time, stay function-scoped, and never reach
 * caches, notify lines, or error output — only the NAMES of missing env vars are reported.
 */

import { readFileSync, readdirSync, existsSync, mkdirSync, writeFileSync } from "node:fs";
import { join, basename, dirname } from "node:path";
import type { PluginActionContext } from "../../types.js";
import { loadStoreConfig, resolvePackagePath, writeBackAddonStoreId, STORE_CONFIG_FILE } from "./config.js";
import type { StoreApp, StoreConfig } from "./config.js";
import { aiAvailable, aiComplete } from "../../../runners/ai.js";
import {
  buildListingSystem,
  buildListingUser,
  buildListingPrompt,
  parseListing,
  normalizeListing,
  type ListingGenContext,
} from "./engine/listinggen.js";
import { resolveCreds, CRED_ENV_NAMES } from "./engine/creds.js";
import { acquireToken, createClient, type StoreClient } from "./engine/client.js";
import { submitApp, type SubmitResult } from "./engine/submission.js";
import { ensureAddon } from "./engine/addon.js";
import { fetchAppStatus, collectAddonsByApp, buildStatusCache, type StatusCache, type StatusRow } from "./engine/status.js";
import { fetchAnalyticsPaged, summarizeRevenue, mergeAnalyticsCache, type AnalyticsCache, type AnalyticsCacheEntry } from "./engine/analytics.js";
import { fetchListing, mergeMetadata } from "./engine/listing.js";
import { usdToTier, tierToUsd } from "./engine/pricing.js";
import { probeImageSize, evaluateScreenshot } from "./engine/images.js";
import { SCREENSHOT_LIMITS } from "./engine/limits.js";
import type { EngineIO, PublishMode, StoreMetadata, UploadFile } from "./engine/types.js";

/* ── shared plumbing ───────────────────────────────────────────────────────────── */

/**
 * The reliable pricing scheme for an app — read from the PUBLISHED app submission, since a fresh
 * add-on submission misreports `isAdvancedPricingModel` (see engine/pricing.ts). Defaults to
 * advanced (the current scheme) when it can't be determined.
 */
async function resolveAdvancedModel(client: StoreClient, appId: string): Promise<boolean> {
  try {
    const meta = await client.request<{ lastPublishedApplicationSubmission?: { id?: string } }>("GET", `applications/${appId}`);
    const subId = meta.lastPublishedApplicationSubmission?.id;
    if (!subId) return true;
    const sub = await client.request<{ pricing?: { isAdvancedPricingModel?: boolean } }>("GET", `applications/${appId}/submissions/${subId}`);
    const v = sub.pricing?.isAdvancedPricingModel;
    return v === undefined || v === null ? true : Boolean(v);
  } catch {
    return true;
  }
}

function makeIO(a: PluginActionContext): EngineIO {
  const c = a.theme.c;
  return {
    fetch: globalThis.fetch,
    sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
    now: () => Date.now(),
    log: (m) => console.log("  " + c.dim(m)),
    warn: (m) => console.log("  " + c.yellow("!! " + m)),
  };
}

/**
 * Non-interactive when either stream isn't a TTY (CI, `navig-menu run <action>`
 * with piped stdio). enquirer prompts never resolve in that case, so publish /
 * addon must read their choices from the environment instead of prompting.
 */
function nonInteractive(): boolean {
  return !process.stdin.isTTY || !process.stdout.isTTY;
}

/**
 * Mode picker with a headless fallback: interactive → the real select; else read
 * `envVar` (validated against the choice names, `fallback` when unset). Returns
 * undefined on an invalid env value so the caller aborts cleanly.
 */
async function selectMode(
  a: PluginActionContext,
  message: string,
  choices: { name: string; message: string; hint?: string }[],
  envVar: string,
  fallback: string,
): Promise<string | undefined> {
  if (!nonInteractive()) return a.select({ message, choices });
  const want = (process.env[envVar] ?? fallback).trim();
  if (!choices.some((ch) => ch.name === want)) {
    a.notify(`${envVar}="${want}" is not one of: ${choices.map((c) => c.name).join(", ")}`);
    return undefined;
  }
  return want;
}

/** Load config + creds + token. Notifies and returns undefined on any gap (names only). */
async function connect(a: PluginActionContext): Promise<{ client: StoreClient; config: StoreConfig; io: EngineIO } | undefined> {
  const io = makeIO(a);
  const loaded = loadStoreConfig(a.root);
  if (!loaded.ok) {
    a.notify(loaded.error);
    return undefined;
  }
  if (!loaded.config.apps.length) {
    a.notify(`${STORE_CONFIG_FILE} has no apps configured`);
    return undefined;
  }
  const creds = resolveCreds(a.root, loaded.config.credsFile);
  if (!creds.ok) {
    a.notify(`missing credentials: ${creds.missing.join(" / ")} — ${creds.hint}`);
    return undefined;
  }
  try {
    const token = await acquireToken(creds.creds, io);
    return { client: createClient(token, io), config: loaded.config, io };
  } catch (e) {
    a.notify((e as Error).message.split("\n")[0] ?? "authentication failed");
    return undefined;
  }
}

/** Pick one app — or return it directly when only one is configured (single-app first-class). */
async function pickApp(a: PluginActionContext, config: StoreConfig, opts?: { allowAll?: string }): Promise<StoreApp | "all" | undefined> {
  if (config.apps.length === 1) return config.apps[0]; // "ALL" of one app is that app
  if (nonInteractive()) {
    const want = (process.env.NAVIG_STORE_APP ?? "").trim();
    if (opts?.allowAll && want === "all") return "all";
    const found = config.apps.find((app) => app.id === want);
    if (!found) a.notify(`multiple apps — set NAVIG_STORE_APP to one of: ${config.apps.map((x) => x.id).join(", ")}`);
    return found;
  }
  const status = a.readCache<StatusCache>("status");
  const choices = config.apps.map((app) => ({
    name: app.id,
    message: app.name,
    hint: status?.perApp?.[app.id]?.state ?? app.appId,
  }));
  if (opts?.allowAll) choices.push({ name: "__all", message: opts.allowAll, hint: "" });
  const picked = await a.select({ message: "Which app?", choices });
  if (picked === undefined) return undefined;
  if (picked === "__all") return "all";
  return config.apps.find((app) => app.id === picked);
}

function pollTimeoutMs(a: PluginActionContext): number {
  const v = typeof a.settings.pollTimeout === "string" ? a.settings.pollTimeout : "30m";
  const m = /^(\d+)m$/.exec(v);
  return (m ? Number(m[1]) : 30) * 60_000;
}

/* ── publish ───────────────────────────────────────────────────────────────────── */

type PublishFlavor = { key: "dry" | "package" | "manual" | "live" | "rebuild"; publishMode: PublishMode; dryRun: boolean; packageOnly: boolean; forceBuild?: boolean };

const FLAVORS: Record<string, PublishFlavor> = {
  dry: { key: "dry", publishMode: "Manual", dryRun: true, packageOnly: false },
  package: { key: "package", publishMode: "Manual", dryRun: false, packageOnly: true },
  manual: { key: "manual", publishMode: "Manual", dryRun: false, packageOnly: false },
  live: { key: "live", publishMode: "Immediate", dryRun: false, packageOnly: false },
  // Rebuild first, then submit under the manual gate (like the legacy "Ship" flow).
  rebuild: { key: "rebuild", publishMode: "Manual", dryRun: false, packageOnly: false, forceBuild: true },
};

/**
 * Run an app's `build` command (bump + compile + pack). A bare word is an npm script; anything with
 * a space runs verbatim as argv (no shell). Returns the exit code.
 */
async function runBuild(a: PluginActionContext, build: string): Promise<number> {
  const trimmed = build.trim();
  if (/\s/.test(trimmed)) {
    const parts = trimmed.split(/\s+/);
    return a.run(parts[0]!, parts.slice(1));
  }
  const pm = a.manifest.packageManager.value;
  return a.run(pm === "none" ? "npm" : pm, ["run", trimmed]);
}

export async function publish(a: PluginActionContext): Promise<void> {
  const c = a.theme.c;
  const ctx = await connect(a);
  if (!ctx) return;
  const { client, config, io } = ctx;

  const picked = await pickApp(a, config, config.apps.length > 1 ? { allowAll: "ALL published apps" } : undefined);
  if (!picked) return a.notify("cancelled");

  const dryFirst = a.settings.dryRunDefault !== false;
  const modeChoices = [
    { name: "dry", message: "Dry run", hint: "prepare + upload, no commit — review in Partner Center" },
    { name: "manual", message: "Submit (manual go-live)", hint: "commit → certification; you click Publish" },
    { name: "package", message: "Package only", hint: "updates only — skip listing/screenshots" },
    { name: "live", message: "Go live (Immediate)", hint: "fully hands-off — publishes when certified" },
  ];
  // Offer a rebuild-first "ship" mode when the target(s) declare a build command.
  const canBuild = picked === "all" ? config.apps.some((x) => x.build) : Boolean(picked.build);
  if (canBuild) modeChoices.push({ name: "rebuild", message: "Rebuild & submit", hint: "run the app's build (bump + compile + package), then submit" });
  if (!dryFirst) modeChoices.push(modeChoices.shift()!);
  const modeKey = await selectMode(a, "Publish mode?", modeChoices, "NAVIG_STORE_PUBLISH_MODE", "dry");
  if (!modeKey) return a.notify("cancelled");
  const flavor = FLAVORS[modeKey]!;

  // Go-live is gated by a TYPED confirmation (internal actions bypass the framework's risk
  // gate, so this must live in the handler; it never consults --yes by construction).
  // Headless, the same confirmation is required via NAVIG_STORE_CONFIRM.
  if (flavor.key === "live") {
    const expected = picked === "all" ? "all" : picked.id;
    const typed = nonInteractive()
      ? (process.env.NAVIG_STORE_CONFIRM ?? "").trim()
      : await a.input(`Type "${expected}" to confirm IMMEDIATE go-live after certification:`);
    if (typed !== expected) return a.notify(`go-live not confirmed — aborted (headless: set NAVIG_STORE_CONFIRM="${expected}")`);
  }

  let targets: StoreApp[];
  if (picked === "all") {
    // "ALL" means all PUBLISHED apps — never batch a first publish silently.
    const addonByApp = await safeAddons(client, io);
    targets = [];
    for (const app of config.apps) {
      const row = await fetchAppStatus(client, { id: app.id, name: app.name, appId: app.appId, needsAddon: Boolean(app.addon) }, addonByApp);
      if (row.published) targets.push(app);
      else io.log(`skipping ${app.name} — never published (publish it individually first)`);
    }
    if (!targets.length) return a.notify("no published apps to update");
  } else {
    targets = [picked];
  }

  let ok = 0;
  let failed = 0;
  for (const app of targets) {
    console.log("\n  " + c.cyanBright.bold(app.name) + c.dim(`  (${app.appId})`));
    try {
      const result = await publishOne(a, client, io, config, app, flavor);
      printResult(a, result);
      if (result.outcome === "blocked") failed++;
      else ok++;
    } catch (e) {
      failed++;
      console.log("  " + c.red((e as Error).message));
    }
  }
  a.notify(
    targets.length === 1
      ? failed
        ? `publish failed — see output above`
        : `publish ${flavor.dryRun ? "dry run " : ""}done for ${targets[0]!.name}`
      : `publish finished: ${ok} ok, ${failed} failed of ${targets.length}`,
  );
}

async function publishOne(
  a: PluginActionContext,
  client: StoreClient,
  io: EngineIO,
  config: StoreConfig,
  app: StoreApp,
  flavor: PublishFlavor,
): Promise<SubmitResult> {
  // Build first when asked (rebuild mode), or auto-build when no package exists yet and the app
  // declares how to build one — mirrors the legacy "no package → build it" flow. The build command
  // owns version-bump + compile + pack.
  let pkg = resolvePackagePath(a.root, app.package);
  const needsBuild = flavor.forceBuild || "error" in pkg;
  if (needsBuild && app.build) {
    io.log(flavor.forceBuild ? `rebuilding via "${app.build}"…` : `no package matches ${app.package} — building via "${app.build}"…`);
    const code = await runBuild(a, app.build);
    if (code !== 0) throw new Error(`build failed ("${app.build}", exit ${code}) — see output above`);
    pkg = resolvePackagePath(a.root, app.package);
  } else if (flavor.forceBuild && !app.build) {
    io.warn(`no build command configured (apps.${app.id}.build) — submitting the existing package`);
  }
  if ("error" in pkg) throw new Error(pkg.error + (app.build ? "" : ` — set apps.${app.id}.build to auto-build it`));
  const packageName = basename(pkg.path);
  const packageData = new Uint8Array(readFileSync(pkg.path));
  io.log(`package: ${packageName} (${(packageData.length / 1024 / 1024).toFixed(1)} MB)`);

  let metadata: StoreMetadata | undefined;
  let screenshots: UploadFile[] = [];
  if (!flavor.packageOnly) {
    if (app.metadata) {
      const mp = join(a.root, app.metadata);
      if (existsSync(mp)) {
        try {
          metadata = JSON.parse(readFileSync(mp, "utf8")) as StoreMetadata;
        } catch {
          throw new Error(`metadata file ${app.metadata} is not valid JSON — fix it before publishing`);
        }
      } else io.warn(`metadata file not found: ${app.metadata} — submitting without listing changes`);
    }
    if (app.screenshots) {
      screenshots = collectScreenshots(a, io, join(a.root, app.screenshots));
      // Safety: never silently drop live screenshots the local folder doesn't have.
      if (screenshots.length) {
        try {
          const live = await fetchListing(client, app.appId);
          if (live.screenshots.length > screenshots.length) {
            // Headless default is the safe one: never silently drop live screenshots.
            const okReplace = nonInteractive()
              ? false
              : await a.confirm(
                  `live listing has ${live.screenshots.length} screenshots but only ${screenshots.length} local — replacing drops the extras. Continue?`,
                );
            if (!okReplace) {
              io.warn("keeping live screenshots — submitting without screenshot changes");
              screenshots = [];
            }
          }
        } catch {
          /* listing unreadable (e.g. first publish) — proceed */
        }
      }
    }
  }

  return submitApp(client, io, {
    appId: app.appId,
    packageName,
    packageData,
    metadata,
    screenshots,
    publishMode: flavor.publishMode,
    certNotes: app.notesForCertification ?? config.defaults?.notesForCertification,
    category: app.category ?? config.defaults?.category,
    dryRun: flavor.dryRun,
    replacePending: false,
    pollTimeoutMs: pollTimeoutMs(a),
  });
}

function collectScreenshots(a: PluginActionContext, io: EngineIO, dir: string): UploadFile[] {
  if (!existsSync(dir)) {
    io.warn(`screenshots dir not found: ${dir}`);
    return [];
  }
  const files = readdirSync(dir)
    .filter((n) => SCREENSHOT_LIMITS.formats.some((ext) => n.toLowerCase().endsWith("." + ext)))
    .sort()
    .slice(0, SCREENSHOT_LIMITS.count.max);
  const ready: UploadFile[] = [];
  for (const name of files) {
    const data = new Uint8Array(readFileSync(join(dir, name)));
    const size = probeImageSize(data);
    if (!size) {
      io.warn(`skipping ${name} — not a readable PNG/JPEG`);
      continue;
    }
    const verdict = evaluateScreenshot({ name, bytes: data.length, width: size.width, height: size.height });
    for (const w of verdict.warnings) io.warn(w);
    if (verdict.verdict === "skip") {
      io.warn(verdict.reason!);
      continue;
    }
    if (verdict.verdict === "oversize") {
      const { maxW, maxH } = SCREENSHOT_LIMITS.px;
      io.warn(`${verdict.reason} — e.g.: magick "${name}" -resize ${maxW}x${maxH} "${name}"`);
      continue;
    }
    ready.push({ name, data });
  }
  return ready;
}

function printResult(a: PluginActionContext, result: SubmitResult): void {
  const c = a.theme.c;
  if (result.checklist?.length) {
    console.log("");
    for (const line of result.checklist) console.log("  " + c.yellow(line));
  }
  const label: Record<SubmitResult["outcome"], string> = {
    accepted: "submission accepted — in certification",
    committed: "committed",
    prepared: "prepared — finish the checklist above, then re-run",
    "dry-run": "dry run complete — review the draft in Partner Center",
    blocked: "blocked — see above",
  };
  console.log("  " + (result.outcome === "blocked" ? c.red(label[result.outcome]) : c.green(label[result.outcome])));
}

async function safeAddons(client: StoreClient, io: EngineIO): Promise<Record<string, boolean>> {
  try {
    return await collectAddonsByApp(client);
  } catch (e) {
    io.warn(`could not list add-ons: ${(e as Error).message.split("\n")[0]}`);
    return {};
  }
}

/* ── status ────────────────────────────────────────────────────────────────────── */

export async function status(a: PluginActionContext): Promise<void> {
  const c = a.theme.c;
  const ctx = await connect(a);
  if (!ctx) {
    // No config/creds → degrade to the cache-age report the skeleton offered.
    const s = a.readCache<{ generatedAt?: number }>("stats") ?? a.readCache<{ generatedAt?: number }>("store.stats");
    a.notify(
      s?.generatedAt ? `store cache from ${new Date(s.generatedAt).toISOString().slice(0, 10)}` : "no store cache yet — run Refresh stats",
    );
    return;
  }
  const { client, config, io } = ctx;
  const addonByApp = await safeAddons(client, io);

  const rows: StatusRow[] = [];
  for (const app of config.apps) {
    rows.push(await fetchAppStatus(client, { id: app.id, name: app.name, appId: app.appId, needsAddon: Boolean(app.addon) }, addonByApp));
  }

  const nameW = Math.min(Math.max(...rows.map((r) => r.name.length)) + 2, 34);
  console.log("\n  " + c.cyanBright("Microsoft Store — submission status"));
  console.log("  " + c.dim("─".repeat(nameW + 34)));
  for (const r of rows) {
    const tone = /Published/.test(r.state) ? c.green : /In progress/.test(r.state) ? c.yellow : /FAILED|ERROR/.test(r.state) ? c.red : c.dim;
    const addonTxt = !r.needsAddon ? "free" : r.hasAddon ? "+ add-on" : "NO ADD-ON";
    console.log("  " + r.name.slice(0, nameW - 1).padEnd(nameW) + tone(r.state.padEnd(18)) + c.dim(addonTxt.padEnd(11)) + c.dim(r.status));
    if (r.certReport) console.log("      " + c.dim(`cert report: ${r.certReport}`));
    for (const e of r.errors) console.log("      " + c.red(e));
  }

  a.writeCache("status", buildStatusCache(rows, a.readCache<StatusCache>("status"), Date.now()));
  const live = rows.filter((r) => r.published).length;
  a.notify(`status refreshed: ${live}/${rows.length} live`);
}

/* ── analytics ─────────────────────────────────────────────────────────────────── */

export async function refreshStats(a: PluginActionContext): Promise<void> {
  const c = a.theme.c;
  const ctx = await connect(a);
  if (!ctx) {
    // Legacy delegation (skeleton behavior): run the project's own stats script if present.
    const key = a.scripts["store:stats"] ? "store:stats" : a.scripts["revenue"] ? "revenue" : undefined;
    if (key) {
      const pm = a.manifest.packageManager.value;
      await a.run(pm === "none" ? "npm" : pm, ["run", key]);
      a.notify("store stats refreshed (project script)");
    }
    return;
  }
  const { client, config, io } = ctx;
  const storeCut = config.defaults?.storeCut ?? 0.15;
  const end = new Date().toISOString().slice(0, 10);
  const start = new Date(Date.now() - 3 * 365 * 24 * 3600 * 1000).toISOString().slice(0, 10);
  const monthStart = end.slice(0, 8) + "01";

  const perApp: Record<string, AnalyticsCacheEntry> = {};
  let failures = 0;
  if (config.apps.length > 1) io.log(`fetching analytics for ${config.apps.length} apps — the Store rate-limits this endpoint, expect ~10s per app`);
  client.pace(6000); // analytics is heavily rate-limited — pace from the first call so the sweep doesn't 429 per request
  for (const app of config.apps) {
    try {
      io.log(`querying ${app.name}…`);
      const appRows = await fetchAnalyticsPaged(client, io, "acquisitions", {
        applicationId: app.appId,
        startDate: start,
        endDate: end,
        aggregationLevel: "day",
        groupby: "date",
      });
      const addonRows = app.addon?.storeId
        ? await fetchAnalyticsPaged(client, io, "inappacquisitions", {
            applicationId: app.appId,
            inAppProductIdList: app.addon.storeId,
            startDate: start,
            endDate: end,
            aggregationLevel: "day",
            groupby: "date",
          })
        : [];
      const s = summarizeRevenue(appRows, addonRows, monthStart, storeCut);
      perApp[app.id] = {
        name: app.name,
        downloads: s.downloads,
        sales: s.units,
        gross: s.revBase,
        net: s.estNet,
        mDownloads: s.month?.downloads ?? 0,
        mSales: s.month?.units ?? 0,
        mGross: s.month?.revBase ?? 0,
        mNet: s.month?.estNet ?? 0,
      };
      console.log(
        "  " + app.name.padEnd(26) + String(s.downloads).padStart(10) + c.dim(" downloads") + ("$" + s.estNet.toFixed(2)).padStart(12) + c.dim(" est net"),
      );
    } catch (e) {
      failures++;
      io.warn(`${app.name}: ${(e as Error).message.split("\n")[0]}`);
    }
  }

  const merged = mergeAnalyticsCache(a.readCache<AnalyticsCache>("stats"), perApp, storeCut, Date.now());
  a.writeCache("stats", merged);
  console.log(
    "\n  " +
      c.green(
        `TOTAL: ${merged.totals.downloads.toLocaleString()} downloads · $${merged.totals.net.toFixed(2)} est net · $${merged.month.net.toFixed(2)} this month`,
      ),
  );
  if (failures) io.warn(`${failures} app(s) returned no data — kept last-known values in the cache`);
  a.notify(`stats refreshed for ${Object.keys(perApp).length}/${config.apps.length} app(s)`);
}

/* ── prices audit ─────────────────────────────────────────────────────────────── */

export async function prices(a: PluginActionContext): Promise<void> {
  const c = a.theme.c;
  const ctx = await connect(a);
  if (!ctx) return;
  const { client, config } = ctx;

  console.log("\n  " + "APP".padEnd(18) + "LOCAL".padEnd(9) + "LIVE".padEnd(9) + "TIER".padEnd(11) + "STATUS");
  console.log("  " + c.dim("─".repeat(70)));
  let mismatches = 0;
  for (const app of config.apps) {
    const local = app.addon?.priceUsd ?? 0;
    const addonId = app.addon?.storeId?.trim() ?? "";
    let liveUsd: number | null = null;
    let liveTier = "";
    let statusTxt: string;
    let tone = c.dim;

    if (!app.addon) {
      statusTxt = "free (no add-on)";
    } else if (!addonId) {
      statusTxt = "no add-on yet (run: Ensure add-on)";
      tone = c.yellow;
    } else {
      try {
        const iap = await client.request<{ lastPublishedInAppProductSubmission?: { id?: string }; pendingInAppProductSubmission?: { id?: string } }>(
          "GET",
          `inappproducts/${addonId}`,
        );
        const published = Boolean(iap.lastPublishedInAppProductSubmission?.id);
        const subRef = iap.lastPublishedInAppProductSubmission?.id ?? iap.pendingInAppProductSubmission?.id;
        if (!subRef) {
          statusTxt = "add-on has no submission yet";
          tone = c.yellow;
        } else {
          const sub = await client.request<{ pricing?: { priceId?: string } }>(
            "GET",
            `inappproducts/${addonId}/submissions/${subRef}`,
          );
          liveTier = sub.pricing?.priceId ?? "";
          // Read the scheme from the PUBLISHED APP submission — the add-on submission's
          // isAdvancedPricingModel is unreliable and would produce false MISMATCH / negative $.
          const advanced = await resolveAdvancedModel(client, app.appId);
          const localTier = usdToTier(local, advanced).tier;
          if (/^Tier\d+$/.test(liveTier)) {
            liveUsd = tierToUsd(liveTier, advanced);
            if (liveTier === localTier) {
              statusTxt = "OK (in sync)";
              tone = c.green;
            } else {
              statusTxt = `MISMATCH — app shows $${local.toFixed(2)}, live charges ${liveUsd === null ? "?" : "$" + liveUsd.toFixed(2)}`;
              tone = c.red;
              mismatches++;
            }
          } else if (published) {
            statusTxt = "LIVE & priced in Partner Center (exact tier not exposed by this API)";
            tone = c.green;
          } else {
            statusTxt = `no base price set yet — set $${local.toFixed(2)} once in Partner Center, then publish`;
            tone = c.yellow;
          }
        }
      } catch (e) {
        statusTxt = `API error: ${(e as Error).message.split("\n")[0]}`;
        tone = c.red;
      }
    }
    console.log(
      "  " +
        app.id.padEnd(18) +
        ("$" + local.toFixed(2)).padEnd(9) +
        (liveUsd === null ? "—" : "$" + liveUsd.toFixed(2)).padEnd(9) +
        (liveTier || "—").padEnd(11) +
        tone(statusTxt),
    );
  }
  a.notify(mismatches ? `${mismatches} price mismatch(es) — see table` : "prices audited — no conflicts in comparable add-ons");
}

/* ── add-on ────────────────────────────────────────────────────────────────────── */

export async function addon(a: PluginActionContext): Promise<void> {
  const c = a.theme.c;
  const ctx = await connect(a);
  if (!ctx) return;
  const { client, config, io } = ctx;

  const withAddon = config.apps.filter((app) => app.addon);
  if (!withAddon.length) return a.notify(`no apps with an addon block in ${STORE_CONFIG_FILE}`);
  const picked = await pickApp(a, { ...config, apps: withAddon }, undefined);
  if (!picked || picked === "all") return a.notify("cancelled");

  const mode = await selectMode(
    a,
    "Add-on mode?",
    [
      { name: "dry", message: "Dry run", hint: "prepare, no commit" },
      { name: "manual", message: "Submit (manual go-live)" },
      { name: "live", message: "Go live (Immediate)" },
    ],
    "NAVIG_STORE_ADDON_MODE",
    "dry",
  );
  if (!mode) return a.notify("cancelled");
  if (mode === "live") {
    const typed = nonInteractive()
      ? (process.env.NAVIG_STORE_CONFIRM ?? "").trim()
      : await a.input(`Type "${picked.id}" to confirm IMMEDIATE add-on go-live:`);
    if (typed !== picked.id) return a.notify(`go-live not confirmed — aborted (headless: set NAVIG_STORE_CONFIRM="${picked.id}")`);
  }

  const result = await ensureAddon(client, io, {
    appId: picked.appId,
    identity: picked.identity,
    productId: picked.addon!.productId,
    configuredStoreId: picked.addon!.storeId,
    priceUsd: picked.addon!.priceUsd,
    title: picked.addon!.title,
    description: picked.addon!.description,
    appName: picked.name,
    publishMode: mode === "live" ? "Immediate" : "Manual",
    dryRun: mode === "dry",
    pollTimeoutMs: pollTimeoutMs(a),
  });

  if (result.checklist?.length) {
    console.log("");
    for (const line of result.checklist) console.log("  " + c.yellow(line));
  }
  if (result.writeBackStoreId && result.writeBackStoreId !== picked.addon!.storeId) {
    // Headless: write the freshly-created Store ID back automatically (the point of the run).
    const okWrite = nonInteractive() ? true : await a.confirm(`write add-on Store ID ${result.writeBackStoreId} back into ${STORE_CONFIG_FILE}?`);
    if (okWrite) {
      if (writeBackAddonStoreId(a.root, picked.id, result.writeBackStoreId)) io.log(`updated ${STORE_CONFIG_FILE} (apps.${picked.id}.addon.storeId)`);
      else io.warn(`could not update ${STORE_CONFIG_FILE} — set apps.${picked.id}.addon.storeId = ${result.writeBackStoreId} manually`);
    } else {
      io.warn(`remember to set apps.${picked.id}.addon.storeId = ${result.writeBackStoreId} (and mirror it into your project config)`);
    }
  }
  a.notify(`add-on ${result.outcome}${result.storeId ? ` (${result.storeId})` : ""}`);
}

/* ── listing pull ─────────────────────────────────────────────────────────────── */

export async function pullListing(a: PluginActionContext): Promise<void> {
  const c = a.theme.c;
  const ctx = await connect(a);
  if (!ctx) return;
  const { client, config, io } = ctx;

  const withMeta = config.apps.filter((app) => app.metadata);
  if (!withMeta.length) return a.notify(`no apps with a metadata path in ${STORE_CONFIG_FILE}`);
  const picked = await pickApp(a, { ...config, apps: withMeta }, undefined);
  if (!picked || picked === "all") return a.notify("cancelled");

  let live;
  try {
    live = await fetchListing(client, picked.appId);
  } catch (e) {
    return a.notify((e as Error).message.split("\n")[0] ?? "listing fetch failed");
  }
  io.log(`live listing: ${live.primaryName ?? picked.appId} — ${live.screenshots.length} screenshot(s) (image bytes are not downloadable via this API)`);

  const metaPath = join(a.root, picked.metadata!);
  let local: StoreMetadata = {};
  if (existsSync(metaPath)) {
    try {
      local = JSON.parse(readFileSync(metaPath, "utf8")) as StoreMetadata;
    } catch {
      return a.notify(`${picked.metadata} is not valid JSON — fix it before pulling the listing`);
    }
  }

  const mode = await a.select({
    message: "Merge mode?",
    choices: [
      { name: "merge", message: "Fill empty fields (local wins)", hint: "protects unsubmitted local edits" },
      { name: "force", message: "Force overwrite (Store wins)", hint: "full pull of the live listing" },
    ],
  });
  if (!mode) return a.notify("cancelled");

  const { merged, changes } = mergeMetadata(local, live, mode === "force");
  if (!changes.length) return a.notify("local metadata already matches the live listing");
  console.log("\n  " + c.cyanBright("changes: ") + changes.join(", "));
  const okWrite = await a.confirm(`write ${changes.length} field(s) to ${picked.metadata}?`);
  if (!okWrite) return a.notify("cancelled — nothing written");
  const { writeFileSync, mkdirSync } = await import("node:fs");
  const { dirname } = await import("node:path");
  mkdirSync(dirname(metaPath), { recursive: true });
  writeFileSync(metaPath, JSON.stringify(merged, null, 2) + "\n", "utf8");
  a.notify(`updated ${picked.metadata}: ${changes.join(", ")}`);
}

/* ── listing metadata generation (AI) ───────────────────────────────────────────── */

/** Humanize a package identity's first segment for a copyright/dev-studio default. */
function humanizeStudio(identity?: string): string | undefined {
  const seg = identity?.split(".")[0];
  if (!seg) return undefined;
  const spaced = seg.replace(/([a-z0-9])([A-Z])/g, "$1 $2").trim();
  return spaced || undefined;
}

/** Assemble the ONLY facts the model may use: package description, README, cert notes, add-on. */
function gatherListingContext(a: PluginActionContext, app: StoreApp, config: StoreConfig): ListingGenContext {
  const bits: string[] = [];

  try {
    const pj = JSON.parse(readFileSync(join(a.root, "package.json"), "utf8")) as { description?: string };
    if (pj.description) bits.push(`Package description: ${pj.description}`);
  } catch {
    /* no package.json / unreadable — fine */
  }

  for (const name of ["README.md", "readme.md", "Readme.md"]) {
    const p = join(a.root, name);
    if (existsSync(p)) {
      try {
        bits.push(`README (excerpt):\n${readFileSync(p, "utf8").slice(0, 6000)}`);
      } catch {
        /* unreadable — skip */
      }
      break;
    }
  }

  const notes = app.notesForCertification ?? config.defaults?.notesForCertification;
  if (notes) bits.push(`What the app is (from certification notes, incl. free vs paid):\n${notes}`);
  if (app.addon?.description) bits.push(`Paid add-on "${app.addon.title ?? "Pro"}": ${app.addon.description}`);

  const stack = ["Windows", "MSIX desktop app"];
  if (a.hasFile("tauri-ui/src-tauri/tauri.conf.json") || a.hasFile("src-tauri/tauri.conf.json")) stack.push("Tauri");
  if (a.hasFile("Cargo.toml")) stack.push("Rust");

  return {
    name: app.name,
    identity: app.identity,
    category: app.category ?? config.defaults?.category,
    stack,
    context: bits.join("\n\n").slice(0, 8000) || `A Windows desktop app named ${app.name}.`,
    year: new Date().getFullYear(),
    studio: humanizeStudio(app.identity),
  };
}

/** Zero-config fallback: drive the local `claude` CLI (Claude Code) when no provider key is set. */
async function tryClaudeCli(a: PluginActionContext, prompt: string): Promise<Record<string, unknown> | null> {
  try {
    const { stdout, exitCode } = await a.capture("claude", ["-p", "--output-format", "text", prompt]);
    if (exitCode !== 0 || !stdout.trim()) return null;
    return parseListing(stdout);
  } catch {
    return null;
  }
}

function writeListingFile(root: string, rel: string, content: string): void {
  const p = join(root, rel);
  mkdirSync(dirname(p), { recursive: true });
  writeFileSync(p, content, "utf8");
}

/** Link `apps.<id>.metadata` in store.config.json (so the listing is picked up on publish). */
function setConfigMetadataPath(root: string, appId: string, rel: string): boolean {
  const path = join(root, STORE_CONFIG_FILE);
  try {
    const raw = JSON.parse(readFileSync(path, "utf8")) as { apps?: { id?: string; metadata?: string }[] };
    const app = raw.apps?.find((x) => x.id === appId);
    if (!app) return false;
    app.metadata = rel.replace(/\\/g, "/");
    writeFileSync(path, JSON.stringify(raw, null, 2) + "\n", "utf8");
    return true;
  } catch {
    return false;
  }
}

/**
 * Draft the Store listing (description, features, keywords, what's-new) with AI and write it to the
 * app's `metadata.json`. Provider order: an env provider key (Anthropic/OpenAI) → the local `claude`
 * CLI → a paste-ready prompt file. displayName/category come from config, never the model; every
 * field is hard-clamped to the Store's limits. Non-interactive runs write without a prompt (CI).
 */
export async function metagen(a: PluginActionContext): Promise<void> {
  const c = a.theme.c;
  const loaded = loadStoreConfig(a.root);
  if (!loaded.ok) return a.notify(loaded.error);
  const config = loaded.config;
  if (!config.apps.length) return a.notify(`${STORE_CONFIG_FILE} has no apps configured`);

  const picked = await pickApp(a, config);
  if (!picked || picked === "all") return a.notify("cancelled");

  const ctx = gatherListingContext(a, picked, config);
  console.log("  " + c.dim(`drafting the Store listing for ${picked.name} with AI — this can take a moment…`));

  let raw: Record<string, unknown> | null = null;
  let via = "";
  if (aiAvailable()) {
    const text = await aiComplete(buildListingSystem(), buildListingUser(ctx), 2000);
    raw = text ? parseListing(text) : null;
    if (raw) via = "provider key";
  }
  if (!raw) {
    raw = await tryClaudeCli(a, buildListingPrompt(ctx));
    if (raw) via = "claude CLI";
  }
  if (!raw) {
    const promptRel = join("store", picked.id, "metadata.prompt.txt").replace(/\\/g, "/");
    writeListingFile(a.root, promptRel, buildListingPrompt(ctx));
    return a.notify(
      `no AI provider available (set ANTHROPIC_API_KEY / OPENAI_API_KEY, or install the claude CLI) — wrote ${promptRel}; paste it into any AI and save the JSON reply as the metadata file`,
    );
  }

  const meta = normalizeListing(raw, {
    displayName: picked.name,
    category: picked.category ?? config.defaults?.category,
    year: new Date().getFullYear(),
    studio: humanizeStudio(picked.identity),
  });

  console.log("\n  " + c.cyanBright(`listing draft (via ${via})`));
  if (meta.shortDescription) console.log("  " + c.dim("tagline:  ") + meta.shortDescription);
  if (meta.description) {
    const paras = meta.description.split(/\n\s*\n/).filter(Boolean).length;
    console.log("  " + c.dim("body:     ") + `${meta.description.length} chars · ${paras} paragraph(s)`);
  }
  if (meta.features?.length)
    console.log("  " + c.dim("features: ") + `${meta.features.length} — ` + meta.features.slice(0, 3).join(" · ") + (meta.features.length > 3 ? " …" : ""));
  if (meta.keywords?.length) console.log("  " + c.dim("keywords: ") + meta.keywords.join(", "));

  const rel = (picked.metadata ?? join("store", picked.id, "metadata.json")).replace(/\\/g, "/");
  const interactive = Boolean(process.stdin.isTTY && process.stdout.isTTY);
  const ok = interactive ? await a.confirm(`write this listing to ${rel}? (review + edit before publishing)`) : true;
  if (!ok) return a.notify("cancelled — nothing written");

  writeListingFile(a.root, rel, JSON.stringify(meta, null, 2) + "\n");
  if (!picked.metadata && setConfigMetadataPath(a.root, picked.id, rel)) {
    console.log("  " + c.dim(`linked apps.${picked.id}.metadata → ${rel} in ${STORE_CONFIG_FILE}`));
  }
  a.notify(`listing written to ${rel} — review it, add screenshots, then run Publish`);
}

/* ── init / scaffold ─────────────────────────────────────────────────────────── */

/**
 * Scaffold a `store.config.json` (and a listing stub) for a project that doesn't have one yet —
 * the one-action path to make ANY new MSIX app store-publishable. Detects Tauri / .NET-MSIX build
 * output for sane defaults; the only value it can't know is the Partner Center Store product ID, so
 * it emits a clearly-marked placeholder the user replaces with the id from Partner Center.
 */
export async function initConfig(a: PluginActionContext): Promise<void> {
  const c = a.theme.c;
  const { existsSync } = await import("node:fs");
  const configPath = join(a.root, STORE_CONFIG_FILE);
  if (existsSync(configPath)) {
    const loaded = loadStoreConfig(a.root);
    if (loaded.ok) {
      return a.notify(`${STORE_CONFIG_FILE} already present (${loaded.config.apps.length} app(s): ${loaded.config.apps.map((x) => x.id).join(", ")}) — edit it or run Publish`);
    }
    return a.notify(`${STORE_CONFIG_FILE} exists but is invalid: ${loaded.error}`);
  }

  const { detectAppDefaults, buildScaffoldConfig, buildMetadataStub, APPID_PLACEHOLDER } = await import("./engine/init.js");
  const read = (rel: string): string | undefined => {
    try {
      return readFileSync(join(a.root, rel), "utf8");
    } catch {
      return undefined;
    }
  };
  const detected = detectAppDefaults(read);

  // Reuse an existing gitignored creds file as the fallback, if one is lying around.
  const credsFile = ["scripts/.store-creds.json", ".store-creds.json"].find((p) => existsSync(join(a.root, p)));

  console.log("\n  " + c.cyanBright("Detected") + c.dim(`  (${detected.kind})`));
  console.log("  " + c.dim("name:     ") + detected.name);
  console.log("  " + c.dim("identity: ") + (detected.identity ?? "—"));
  console.log("  " + c.dim("package:  ") + detected.packageGlob);
  console.log("  " + c.dim("build:    ") + detected.buildHint);
  console.log("  " + c.dim("appId:    ") + (detected.appId ?? c.yellow(`${APPID_PLACEHOLDER} (paste the Partner Center Store product ID)`)));

  const interactive = Boolean(process.stdin.isTTY && process.stdout.isTTY);
  if (interactive) {
    const name = (await a.input(`App display name [${detected.name}]:`)) || detected.name;
    detected.name = name;
    const appId = await a.input(`Store product ID (Partner Center, e.g. 9NBLGGH4R2R6) [${detected.appId ?? "leave blank to fill later"}]:`);
    if (appId && appId.trim()) detected.appId = appId.trim();
    const pkg = await a.input(`Package glob [${detected.packageGlob}]:`);
    if (pkg && pkg.trim()) detected.packageGlob = pkg.trim();
  }

  const config = buildScaffoldConfig(detected, { credsFile });
  writeFileSync(configPath, JSON.stringify(config, null, 2) + "\n", "utf8");

  // Drop a listing stub so metagen/publish have a target, unless one already exists.
  const metaRel = config.apps[0]!.metadata!;
  const metaPath = join(a.root, metaRel);
  if (!existsSync(metaPath)) {
    mkdirSync(dirname(metaPath), { recursive: true });
    writeFileSync(metaPath, JSON.stringify(buildMetadataStub(detected), null, 2) + "\n", "utf8");
  }

  console.log("\n  " + c.green(`wrote ${STORE_CONFIG_FILE}`) + c.dim(` + ${metaRel}`));
  const needsId = config.apps[0]!.appId === APPID_PLACEHOLDER;
  console.log("  " + c.dim("next: ") + [
    needsId ? c.yellow(`set apps[0].appId to the Partner Center Store product ID`) : null,
    `run "Generate listing (AI)" for the listing`,
    `build the package (${detected.buildHint.split(" — ")[0]})`,
    `run "Publish / submit" (dry run first)`,
  ].filter(Boolean).join(c.dim(" · ")));
  a.notify(needsId ? `${STORE_CONFIG_FILE} scaffolded — set the Store product ID, then Publish` : `${STORE_CONFIG_FILE} scaffolded for ${detected.name}`);
}

/* ── open Partner Center ─────────────────────────────────────────────────────── */

/** Open the app's Partner Center submissions page in the browser (no auth needed). */
export async function openSubmission(a: PluginActionContext): Promise<void> {
  const loaded = loadStoreConfig(a.root);
  if (!loaded.ok) return a.notify(loaded.error);
  if (!loaded.config.apps.length) return a.notify(`${STORE_CONFIG_FILE} has no apps configured`);
  const picked = await pickApp(a, loaded.config);
  if (!picked || picked === "all") return a.notify("cancelled");
  const url = `https://partner.microsoft.com/dashboard/products/${picked.appId}/submissions`;
  const opener = process.platform === "win32" ? "cmd" : process.platform === "darwin" ? "open" : "xdg-open";
  const argv = process.platform === "win32" ? ["/c", "start", "", url] : [url];
  try {
    await a.run(opener, argv);
    a.notify(`opened Partner Center for ${picked.name}`);
  } catch {
    a.notify(`open manually: ${url}`);
  }
}

/* ── WACK (Windows App Certification Kit) ────────────────────────────────────── */

/** Newest `appcert.exe` from an installed Windows SDK (Windows only). */
function findAppCert(): string | undefined {
  if (process.platform !== "win32") return undefined;
  const bases = [process.env["ProgramFiles(x86)"], process.env.ProgramFiles].filter(Boolean) as string[];
  const found: { path: string; ver: string }[] = [];
  for (const base of bases) {
    const binDir = join(base, "Windows Kits", "10", "bin");
    let vers: string[];
    try {
      vers = readdirSync(binDir);
    } catch {
      continue;
    }
    for (const v of vers) {
      const p = join(binDir, v, "x64", "appcert.exe");
      if (existsSync(p)) found.push({ path: p, ver: v });
    }
  }
  found.sort((a, b) => (a.ver < b.ver ? 1 : -1));
  return found[0]?.path;
}

/**
 * Run the Windows App Certification Kit on the app's built package — the same cert pass the Store
 * runs server-side, so failing here avoids a rejected submission. Tests the .msix/.msixbundle next
 * to the configured `package` (appcert can't test a .msixupload zip). Windows + SDK + elevation.
 */
export async function wack(a: PluginActionContext): Promise<void> {
  const c = a.theme.c;
  if (process.platform !== "win32") return a.notify("WACK is Windows-only (needs the Windows App Certification Kit)");
  const loaded = loadStoreConfig(a.root);
  if (!loaded.ok) return a.notify(loaded.error);
  if (!loaded.config.apps.length) return a.notify(`${STORE_CONFIG_FILE} has no apps configured`);
  const picked = await pickApp(a, loaded.config);
  if (!picked || picked === "all") return a.notify("cancelled");

  const pkg = resolvePackagePath(a.root, picked.package);
  if ("error" in pkg) return a.notify(pkg.error);
  const dir = dirname(pkg.path);
  const { pickWackTarget, parseWackReport } = await import("./engine/wack.js");
  let target: string | undefined;
  try {
    target = pickWackTarget(readdirSync(dir));
  } catch {
    /* dir vanished — handled below */
  }
  if (!target) return a.notify(`no .msix/.msixbundle to certify in ${dir} — build the package first (appcert can't test a .msixupload)`);

  const appcert = findAppCert();
  if (!appcert) return a.notify("appcert.exe not found — install the Windows App Certification Kit (part of the Windows SDK)");

  mkdirSync(a.cacheDir, { recursive: true });
  const reportPath = join(a.cacheDir, `wack-${picked.id}.xml`);
  console.log("  " + c.dim(`WACK: ${target} — several minutes; run from an ELEVATED shell or it fails to launch…`));
  await a.run(appcert, ["reset"]);
  const code = await a.run(appcert, ["test", "-appxpackagepath", join(dir, target), "-reportoutputpath", reportPath]);

  let verdict;
  try {
    verdict = parseWackReport(readFileSync(reportPath, "utf8"));
  } catch {
    return a.notify(code === 0 ? "WACK produced no report — try an elevated shell" : "WACK did not run — try an elevated shell (Run as administrator)");
  }
  const tone = verdict.overall === "PASS" ? c.green : verdict.overall === "WARNING" ? c.yellow : c.red;
  console.log("\n  " + tone(`WACK: ${verdict.overall}`) + c.dim(`  (report: ${reportPath})`));
  for (const f of verdict.failed) console.log("  " + c.red("FAIL  ") + f);
  for (const w of verdict.warnings) console.log("  " + c.yellow("WARN  ") + w);
  a.notify(`WACK ${verdict.overall}${verdict.failed.length ? ` — ${verdict.failed.length} failure(s), see above` : ""}`);
}

/* ── creds check ──────────────────────────────────────────────────────────────── */

export async function checkCreds(a: PluginActionContext): Promise<void> {
  const c = a.theme.c;
  const loaded = loadStoreConfig(a.root);
  const credsFile = loaded.ok ? loaded.config.credsFile : undefined;

  console.log("");
  for (const name of CRED_ENV_NAMES) {
    const set = Boolean(process.env[name]);
    console.log("  " + (set ? c.green("set    ") : c.yellow("missing")) + " " + name);
  }
  console.log("  " + (existsSync(join(a.root, ".env")) ? c.green("present") : c.dim("absent ")) + " .env (root — AZURE_* are also read from here)");
  if (credsFile) {
    console.log("  " + (existsSync(join(a.root, credsFile)) ? c.green("present") : c.yellow("missing")) + ` ${credsFile} (fallback creds file — keep it gitignored)`);
  } else {
    console.log("  " + c.dim("no credsFile configured in " + STORE_CONFIG_FILE + " (env vars only)"));
  }

  const creds = resolveCreds(a.root, credsFile);
  if (!creds.ok) {
    return a.notify(`credentials incomplete: ${creds.missing.join(" / ")} missing`);
  }
  const test = await a.confirm("credentials resolved — test them with a live token request?");
  if (!test) return a.notify(`credentials resolved (source: ${creds.creds.source})`);
  try {
    await acquireToken(creds.creds, makeIO(a));
    a.notify(`auth OK (source: ${creds.creds.source})`);
  } catch (e) {
    a.notify((e as Error).message.split("\n")[0] ?? "auth failed");
  }
}
