/**
 * Store analytics (acquisitions + in-app acquisitions) → downloads / sales / revenue estimate.
 * The analytics API exposes acquisition counts and purchase prices, NOT true net payout — so
 * this is an estimate: revBase = gross − tax (matches Partner Center "revenue"), estNet =
 * revBase × (1 − storeCut). NOTE: the in-app endpoint is `inappacquisitions` (NOT
 * `inappproductacquisitions`) and takes `inAppProductIdList`.
 */

import type { StoreClient } from "./client.js";
import type { ApiObject, EngineIO } from "./types.js";

export interface AcquisitionRow extends ApiObject {
  date?: string;
  acquisitionQuantity?: number;
  purchasePriceUSDAmount?: number;
  purchaseTaxUSDAmount?: number;
}

/** Fetch every row from an analytics endpoint, paging via top/skip (client handles 429/5xx). */
export async function fetchAnalyticsPaged(
  client: StoreClient,
  io: EngineIO,
  resource: string,
  query: Record<string, string>,
  pageSize = 10000,
): Promise<AcquisitionRow[]> {
  const baseQs = Object.entries(query)
    .filter(([, v]) => v !== undefined && v !== "")
    .map(([k, v]) => `${k}=${encodeURIComponent(v)}`)
    .join("&");
  const all: AcquisitionRow[] = [];
  let skip = 0;
  for (;;) {
    const page = await client.request<{ Value?: AcquisitionRow[]; value?: AcquisitionRow[] }>(
      "GET",
      `analytics/${resource}?${baseQs}&top=${pageSize}&skip=${skip}`,
    );
    const rows = page.Value ?? page.value ?? [];
    all.push(...rows);
    if (rows.length < pageSize) break;
    skip += pageSize;
  }
  return all;
}

export interface RevenueMetrics {
  downloads: number;
  units: number;
  gross: number;
  tax: number;
  revBase: number;
  estNet: number;
  appUnits: number;
  addonUnits: number;
}

export interface RevenueSummary extends RevenueMetrics {
  month?: RevenueMetrics;
}

/**
 * Pure: fold daily acquisition rows (app + add-on) into all-time and month-to-date buckets.
 * `monthStart` is a yyyy-MM-dd string; daily rows carry yyyy-MM-dd dates → lexical compare.
 */
export function summarizeRevenue(
  appRows: AcquisitionRow[],
  addonRows: AcquisitionRow[],
  monthStart: string | undefined,
  storeCut: number,
): RevenueSummary {
  const zero = () => ({ downloads: 0, appUnits: 0, appUsd: 0, appTax: 0, addonUnits: 0, addonUsd: 0, addonTax: 0 });
  const all = zero();
  const month = zero();
  const inMonth = (row: AcquisitionRow) => Boolean(monthStart && row.date && String(row.date) >= monthStart);

  for (const row of appRows) {
    const qty = Number(row.acquisitionQuantity ?? 0);
    const price = Number(row.purchasePriceUSDAmount ?? 0) || 0;
    const tax = Number(row.purchaseTaxUSDAmount ?? 0) || 0;
    all.downloads += qty;
    if (inMonth(row)) month.downloads += qty;
    // Paid apps carry a USD price on the app acquisition itself (freemium apps don't).
    if (price > 0) {
      all.appUnits += qty;
      all.appUsd += price;
      all.appTax += tax;
      if (inMonth(row)) {
        month.appUnits += qty;
        month.appUsd += price;
        month.appTax += tax;
      }
    }
  }
  for (const row of addonRows) {
    const qty = Number(row.acquisitionQuantity ?? 0);
    const price = Number(row.purchasePriceUSDAmount ?? 0) || 0;
    const tax = Number(row.purchaseTaxUSDAmount ?? 0) || 0;
    all.addonUnits += qty;
    all.addonUsd += price;
    all.addonTax += tax;
    if (inMonth(row)) {
      month.addonUnits += qty;
      month.addonUsd += price;
      month.addonTax += tax;
    }
  }

  const metrics = (b: ReturnType<typeof zero>): RevenueMetrics => {
    const gross = round2(b.appUsd + b.addonUsd); // what customers paid (may include tax)
    const tax = round2(b.appTax + b.addonTax); // tax MS remits (NOT developer earnings)
    const revBase = round2(gross - tax); // developer revenue base (ex-tax)
    return {
      downloads: b.downloads,
      units: b.appUnits + b.addonUnits,
      gross,
      tax,
      revBase,
      estNet: round2(revBase * (1 - storeCut)),
      appUnits: b.appUnits,
      addonUnits: b.addonUnits,
    };
  };
  return { ...metrics(all), month: monthStart ? metrics(month) : undefined };
}

export interface AnalyticsCacheEntry {
  name: string;
  downloads: number;
  sales: number;
  gross: number;
  net: number;
  mDownloads: number;
  mSales: number;
  mGross: number;
  mNet: number;
}

export interface AnalyticsCache {
  generatedAt: number;
  storeCut: number;
  totals: { downloads: number; sales: number; gross: number; net: number };
  month: { downloads: number; sales: number; gross: number; net: number };
  perApp: Record<string, AnalyticsCacheEntry>;
}

/**
 * Pure: merge fresh per-app figures over the previous cache (apps that failed THIS run keep
 * their last-known-good values instead of dropping to zero) and recompute totals from the
 * merged set — all-time and month alike.
 */
export function mergeAnalyticsCache(
  prev: AnalyticsCache | undefined,
  perApp: Record<string, AnalyticsCacheEntry>,
  storeCut: number,
  nowMs: number,
): AnalyticsCache {
  const merged = { ...(prev?.perApp ?? {}), ...perApp };
  const sum = (k: keyof AnalyticsCacheEntry) => round2(Object.values(merged).reduce((a, s) => a + (Number(s[k]) || 0), 0));
  return {
    generatedAt: nowMs,
    storeCut,
    totals: { downloads: sum("downloads"), sales: sum("sales"), gross: sum("gross"), net: sum("net") },
    month: { downloads: sum("mDownloads"), sales: sum("mSales"), gross: sum("mGross"), net: sum("mNet") },
    perApp: merged,
  };
}

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}
