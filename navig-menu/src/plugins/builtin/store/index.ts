import { definePlugin } from "../../types.js";
import { statsBannerLines, appTableLines } from "./banner.js";
import { STORE_CONFIG_FILE } from "./config.js";
import { publish, status, refreshStats, prices, addon, pullListing, checkCreds, metagen, initConfig, openSubmission, wack } from "./handlers.js";

/**
 * Store publishing pack — a full Microsoft Store (Partner Center) pipeline with a native
 * TypeScript engine: publish/submit (dry-run → go-live), submission status, add-on (IAP)
 * lifecycle + price audit, analytics, and live-listing pulls. Driven entirely by a generic
 * `store.config.json` at the project root (any MSIX app, one or many per repo) — the plugin
 * carries no product-specific defaults. Without that config it degrades to the original
 * skeleton behavior: claims the project's `store:*`/`steam:*` scripts into a STORE rail and
 * delegates stats to project scripts. See docs/store-plugin.md.
 */
export const store = definePlugin({
  id: "store",
  tier: "programmatic",
  version: "2.1.0",
  detect: (ctx) => {
    const names = Object.keys(ctx.scripts);
    if (names.some((n) => /^(store|steam):/.test(n))) return true;
    if (Object.values(ctx.scripts).some((cmd) => /\bmsstore\b/i.test(cmd))) return true;
    if (
      ctx.hasFile(STORE_CONFIG_FILE) ||
      ctx.hasFile(".store-creds.json") ||
      ctx.hasFile("steam.config.json") ||
      ctx.hasFile("scripts/desktop/store.config.json")
    )
      return true;
    // Any Tauri / MSIX desktop app is a plausible Store target — activate so the
    // (config-less) skeleton can offer "Initialize store config". Without this,
    // onboarding a brand-new app is a chicken-and-egg: init creates the config,
    // but the plugin only showed once a config existed.
    return (
      ctx.hasFile("src-tauri/tauri.conf.json") ||
      ctx.hasFile("tauri-ui/src-tauri/tauri.conf.json") ||
      ctx.hasFile("apps/tauri/src-tauri/tauri.conf.json") ||
      ctx.hasFile("AppxManifest.xml") ||
      ctx.hasFile("src/AppxManifest.xml") ||
      ctx.hasFile("src-tauri/Package.appxmanifest")
    );
  },
  contribute: (ctx) => {
    const configured = ctx.hasFile(STORE_CONFIG_FILE);
    const bannerLines = [
      ...(ctx.settings.stat !== false ? statsBannerLines(ctx) : []),
      ...(ctx.settings.table !== false ? appTableLines(ctx) : []),
    ];
    return {
      settings: [
        { key: "stat", label: "Store · banner stat line", type: "toggle", default: true },
        { key: "table", label: "Store · per-app table (multi-app)", type: "toggle", default: true },
        { key: "dryRunDefault", label: "Store · preselect dry-run publish", type: "toggle", default: true },
        { key: "pollTimeout", label: "Store · commit poll timeout", type: "cycle", values: ["15m", "30m", "60m"], default: "30m" },
      ],
      bannerLines,
      // Route the project's own store/steam scripts into the dedicated STORE rail.
      claims: [{ match: "^(store|steam):", group: "Store" }],
      sections: [
        {
          group: "Store",
          meta: { title: "STORE", emoji: "🏪", unicode: "▤", ascii: "$", tone: "magenta" },
          actions: [
            ...(configured
              ? [
                  { id: "store.publish", label: "Publish / submit", internal: "publish", longRunning: true, description: "dry-run · package-only · manual · go-live" },
                  { id: "store.metagen", label: "Generate listing (AI)", internal: "metagen", longRunning: true, description: "AI-draft description, features & keywords → metadata.json (review before publish)" },
                  { id: "store.wack", label: "Certify (WACK)", internal: "wack", longRunning: true, description: "run the Windows App Certification Kit on the built package" },
                  { id: "store.prices", label: "Prices audit", internal: "prices", description: "local config vs live Store tiers (read-only)" },
                  { id: "store.addon", label: "Ensure Pro add-on", internal: "addon", longRunning: true, description: "create/update the durable IAP + price" },
                  { id: "store.listing", label: "Pull live listing", internal: "pullListing", description: "Store listing → local metadata.json" },
                  { id: "store.open", label: "Open Partner Center", internal: "openSubmission", description: "the app's submissions page in the browser" },
                ]
              : [{ id: "store.init", label: "Initialize store config", internal: "initConfig", description: `scaffold ${STORE_CONFIG_FILE} for this app (Tauri / MSIX auto-detected)` }]),
            { id: "store.stats", label: "Refresh stats", internal: "refreshStats", longRunning: true, description: "downloads / revenue → cache" },
            { id: "store.status", label: "Submission status", internal: "status", description: "certification / published state" },
            { id: "store.creds", label: "Check credentials", internal: "checkCreds", description: "Azure AD keys (names only, never values)" },
          ],
        },
      ],
      about: [
        configured
          ? "store plugin — Microsoft Store publishing (native Partner Center engine, store.config.json)"
          : `store plugin — add a ${STORE_CONFIG_FILE} to unlock the full Microsoft Store pipeline (docs/store-plugin.md)`,
      ],
    };
  },
  handlers: {
    publish,
    status,
    refreshStats,
    prices,
    addon,
    pullListing,
    metagen,
    wack,
    initConfig,
    openSubmission,
    checkCreds,
    // Back-compat: the skeleton's creds action key.
    setupCreds: checkCreds,
  },
});
