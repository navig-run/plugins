/**
 * Banner stat lines — pure cache reads (never network). Prefers the plugin's own caches
 * (written by the Refresh stats / Status handlers), falling back to the legacy analytics-cache
 * locations the skeleton supported so existing projects keep their banner.
 */

import { join } from "node:path";
import { readJson } from "../../../detectors/fs.js";
import type { BannerLine, PluginContext, PluginTone } from "../../types.js";
import { STORE_CONFIG_FILE } from "./config.js";
import type { AnalyticsCache } from "./engine/analytics.js";
import type { StatusCache } from "./engine/status.js";

/** Legacy cache shape (project-owned analytics caches from prior-art pipelines). */
interface LegacyStats {
  productName?: string;
  downloads?: number;
  net?: number;
  estNet?: number;
  avgRating?: number | null;
  generatedAt?: number;
  totals?: { downloads?: number; net?: number };
  perSaver?: Record<string, { downloads?: number; net?: number }>;
}

const LEGACY_CACHE_CANDIDATES = ["scripts/desktop/.analytics-cache.json", "scripts/.analytics-cache.json"];

export function statsBannerLines(ctx: PluginContext): BannerLine[] {
  const lines: BannerLine[] = [];

  const own = ctx.readCache<AnalyticsCache>("stats");
  if (own?.perApp && Object.keys(own.perApp).length > 0) {
    const apps = Object.values(own.perApp);
    if (apps.length === 1) {
      const a = apps[0]!;
      lines.push({ text: `Store · ${a.name}: ${fmtInt(a.downloads)} downloads${a.net ? ` · $${a.net.toFixed(2)} net` : ""}`, tone: "green" });
    } else {
      lines.push({
        text: `Store · ${apps.length} apps: ${fmtInt(own.totals.downloads)} downloads · $${own.totals.net.toFixed(2)} net${own.month.net ? ` · $${own.month.net.toFixed(2)} this month` : ""}`,
        tone: "green",
      });
    }
  } else {
    const legacy = loadLegacyStats(ctx);
    if (legacy) lines.push({ text: legacyStatLine(legacy), tone: "green" });
  }

  const status = ctx.readCache<StatusCache>("status");
  if (status?.perApp) {
    const rows = Object.values(status.perApp);
    const inProgress = rows.filter((r) => r.state === "In progress").length;
    const failed = rows.filter((r) => r.state === "FAILED" || r.state === "ERROR").length;
    if (inProgress || failed) {
      const bits = [];
      if (inProgress) bits.push(`${inProgress} in certification`);
      if (failed) bits.push(`${failed} failed`);
      lines.push({ text: `Store · ${bits.join(" · ")}`, tone: failed ? "yellow" : "dim" });
    }
  }

  return lines;
}

export function loadLegacyStats(ctx: PluginContext): LegacyStats | undefined {
  for (const rel of LEGACY_CACHE_CANDIDATES) {
    if (ctx.hasFile(rel)) {
      const raw = readJson<LegacyStats>(join(ctx.root, rel));
      if (raw) return raw;
    }
  }
  return undefined;
}

export function legacyStatLine(s: LegacyStats): string {
  const product = s.productName ?? "store";
  const downloads = s.downloads ?? s.totals?.downloads ?? 0;
  const net = s.net ?? s.estNet ?? s.totals?.net;
  const bits = [`${downloads.toLocaleString()} downloads`];
  if (net) bits.push(`$${net.toFixed(2)} net`);
  if (s.avgRating) bits.push(`★${s.avgRating}`);
  return `Store · ${product}: ${bits.join(" · ")}`;
}

function fmtInt(n: number): string {
  return Number(n || 0).toLocaleString();
}

/**
 * Store status → glyph/word/tone. PUBLISHED wins: a live app reads "live" even while a new
 * submission certifies (that pending update shows in the aggregate "N in certification" line).
 * "no add-on" is only a warning for a *published* app missing its add-on.
 */
function storeStatus(
  st: { published?: boolean; state?: string; hasAddon?: boolean; needsAddon?: boolean } | undefined,
): { word: string; tone: PluginTone; icon: string } {
  if (!st) return { word: "—", tone: "dim", icon: "·" };
  if (st.published) {
    if (st.needsAddon && !st.hasAddon) return { word: "no add-on", tone: "yellow", icon: "▲" };
    return { word: "live", tone: "green", icon: "●" };
  }
  if (st.state === "In progress") return { word: "in cert", tone: "yellow", icon: "◔" };
  if (st.state === "FAILED" || st.state === "ERROR") return { word: "failed", tone: "yellow", icon: "✗" };
  return { word: "unpublished", tone: "dim", icon: "·" };
}

/** Human "N ago" for the cache-freshness line. */
function fetchedAgo(ms: number): string {
  if (!ms) return "never fetched";
  const h = (Date.now() - ms) / 3600000;
  if (h < 1) return `${Math.max(1, Math.round(h * 60))}m ago`;
  if (h < 48) return `${Math.round(h)}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

/**
 * Generic per-app dashboard table — `status · name · price · installs · net`, plus a totals row.
 * Any multi-app MSIX catalog gets it purely from its `store.config.json` (app names + add-on
 * price) merged with the stats/status caches; no project-specific code. Returns [] for a single
 * app (the aggregate stat line already covers that case).
 */
export function appTableLines(ctx: PluginContext): BannerLine[] {
  const config = readJson<{ apps?: Array<{ id: string; name?: string; addon?: { priceUsd?: number } }> }>(
    join(ctx.root, STORE_CONFIG_FILE),
  );
  const apps = config?.apps ?? [];
  if (apps.length < 2) return [];

  const statsCache = ctx.readCache<AnalyticsCache>("stats");
  const statusCache = ctx.readCache<StatusCache>("status");
  const stats = statsCache?.perApp ?? {};
  const status = statusCache?.perApp ?? {};

  const rows = apps.map((app) => {
    const s = storeStatus(status[app.id]);
    const an = stats[app.id];
    return {
      icon: s.icon,
      tone: s.tone,
      name: app.name ?? an?.name ?? app.id,
      word: s.word,
      price: app.addon?.priceUsd != null ? `$${app.addon.priceUsd.toFixed(2)}` : "—",
      dl: Number(an?.downloads ?? 0),
      net: Number(an?.net ?? 0),
    };
  });

  const nameW = Math.min(24, Math.max(5, ...rows.map((r) => r.name.length)));
  const line = (icon: string, name: string, word: string, price: string, dl: string, net: string): string =>
    `${icon} ${name.padEnd(nameW)} ${word.padEnd(12)} ${price.padStart(7)} ${dl.padStart(9)} ${net.padStart(9)}`;
  const money = (n: number): string => `$${Math.round(n).toLocaleString()}`;

  const out: BannerLine[] = [{ text: line(" ", "APP", "STATUS", "PRICE", "INSTALLS", "NET"), tone: "dim" }];
  for (const r of rows) out.push({ text: line(r.icon, r.name, r.word, r.price, fmtInt(r.dl), money(r.net)), tone: r.tone });
  const totDl = rows.reduce((s, r) => s + r.dl, 0);
  const totNet = rows.reduce((s, r) => s + r.net, 0);
  out.push({ text: line(" ", `TOTAL · ${rows.length}`, "", "", fmtInt(totDl), money(totNet)), tone: "green" });
  // installs (stats) and live status refresh separately — show both ages and name which to re-run.
  const statusAgeH = statusCache?.generatedAt ? (Date.now() - statusCache.generatedAt) / 3600000 : Infinity;
  const statsAgeH = statsCache?.generatedAt ? (Date.now() - statsCache.generatedAt) / 3600000 : Infinity;
  const staleRuns: string[] = [];
  if (statsAgeH > 24) staleRuns.push("Refresh stats");
  if (statusAgeH > 24) staleRuns.push("Submission status");
  out.unshift({
    text: `  store data · installs ${fetchedAgo(statsCache?.generatedAt ?? 0)} · status ${fetchedAgo(statusCache?.generatedAt ?? 0)}` +
      (staleRuns.length ? `  ⚠ run ${staleRuns.join(" + ")}` : ""),
    tone: staleRuns.length ? "yellow" : "dim",
  });
  return out;
}
