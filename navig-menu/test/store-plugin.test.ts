import { describe, it, expect } from "vitest";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, utimesSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { unzipSync } from "fflate";
import { scanProject, buildMenuModel, loadDefinition } from "../src/builder/build.js";
import { makeActionContext } from "../src/plugins/apply.js";
import { createTheme } from "../src/ui/theme.js";
import type { MenuModel } from "../src/builder/build.js";

import { usdToTier, tierToUsd } from "../src/plugins/builtin/store/engine/pricing.js";
import { isSasUrlFresh } from "../src/plugins/builtin/store/engine/sas.js";
import { computeRetryDelay, classifyHttpError, createClient, acquireToken } from "../src/plugins/builtin/store/engine/client.js";
import { buildSubmissionZip } from "../src/plugins/builtin/store/engine/zip.js";
import { probeImageSize, evaluateScreenshot } from "../src/plugins/builtin/store/engine/images.js";
import {
  decidePendingAction,
  applyListing,
  applyScreenshots,
  swapPackages,
  ensureCertNotes,
  ensureCategory,
  ensureDeviceFamilies,
  ensureFirstPublishPricing,
  assertFirstPublishHasListing,
  submitApp,
} from "../src/plugins/builtin/store/engine/submission.js";
import { deriveAddonProductId, extractAddonAppIds, shapeAddonSubmission, ensureAddon } from "../src/plugins/builtin/store/engine/addon.js";
import { summarizeRevenue, mergeAnalyticsCache } from "../src/plugins/builtin/store/engine/analytics.js";
import { buildStatusCache } from "../src/plugins/builtin/store/engine/status.js";
import { mergeMetadata } from "../src/plugins/builtin/store/engine/listing.js";
import { loadStoreConfig, resolvePackagePath, writeBackAddonStoreId } from "../src/plugins/builtin/store/config.js";
import { detectAppDefaults, buildScaffoldConfig, slugify, APPID_PLACEHOLDER } from "../src/plugins/builtin/store/engine/init.js";
import { StoreConfigSchema } from "../src/plugins/builtin/store/config.js";
import { pickWackTarget, parseWackReport } from "../src/plugins/builtin/store/engine/wack.js";
import { resolveCreds } from "../src/plugins/builtin/store/engine/creds.js";
import type { EngineIO, ApiObject } from "../src/plugins/builtin/store/engine/types.js";
import type { StatusRow } from "../src/plugins/builtin/store/engine/status.js";

const fx = (name: string) => fileURLToPath(new URL(`./fixtures/${name}`, import.meta.url));

function modelFor(root: string): MenuModel {
  return buildMenuModel(scanProject(root), loadDefinition(root));
}

/* ── engine test harness (mock fetch, no sleeps) ───────────────────────────────── */

type Route = (method: string, url: string, body?: string) => { status: number; body?: unknown; headers?: Record<string, string> } | undefined;

function testIO(routes: Route[]): { io: EngineIO; log: string[]; warn: string[] } {
  const log: string[] = [];
  const warn: string[] = [];
  let clock = 1_000_000;
  const io: EngineIO = {
    fetch: (async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      for (const route of routes) {
        const hit = route(method, url, typeof init?.body === "string" ? init.body : undefined);
        if (hit) {
          const body = typeof hit.body === "string" ? hit.body : hit.body === undefined ? "" : JSON.stringify(hit.body);
          return new Response(body, { status: hit.status, headers: hit.headers });
        }
      }
      throw new Error(`unrouted: ${method} ${url}`);
    }) as typeof globalThis.fetch,
    sleep: async (ms) => {
      clock += ms;
    },
    now: () => clock,
    log: (m) => log.push(m),
    warn: (m) => warn.push(m),
  };
  return { io, log, warn };
}

const api = (path: string) => `https://manage.devcenter.microsoft.com/v1.0/my/${path}`;
const freshSas = "https://blob.example/container/pkg?sv=1&se=2099-01-01T00%3A00%3A00Z&sig=x";

/* ── pricing ───────────────────────────────────────────────────────────────────── */

describe("store engine · pricing", () => {
  it("maps all ten anchors in both schemes", () => {
    const anchors: [number, string, string][] = [
      [0.99, "Tier1012", "Tier2"], [1.99, "Tier1022", "Tier3"], [2.99, "Tier1032", "Tier4"],
      [3.99, "Tier1042", "Tier5"], [4.99, "Tier1052", "Tier6"], [5.99, "Tier1062", "Tier7"],
      [6.99, "Tier1072", "Tier8"], [7.99, "Tier1082", "Tier9"], [8.99, "Tier1092", "Tier10"],
      [9.99, "Tier1102", "Tier11"],
    ];
    for (const [usd, adv, legacy] of anchors) {
      expect(usdToTier(usd, true)).toEqual({ tier: adv, exact: true });
      expect(usdToTier(usd, false)).toEqual({ tier: legacy, exact: true });
    }
  });

  it("advanced formula band steps $0.10 from Tier1012", () => {
    expect(usdToTier(3.09, true).tier).toBe("Tier1033");
    expect(usdToTier(1.09, true).tier).toBe("Tier1013");
  });

  it("out-of-band picks the nearest anchor and flags exact:false", () => {
    const r = usdToTier(14.99, true);
    expect(r.tier).toBe("Tier1102");
    expect(r.exact).toBe(false);
    expect(r.note).toMatch(/nearest anchor/);
    const l = usdToTier(12.5, false);
    expect(l.tier).toBe("Tier11");
    expect(l.exact).toBe(false);
  });

  it("zero/negative → Free; tierToUsd round-trips; Base/garbage → null", () => {
    expect(usdToTier(0, true).tier).toBe("Free");
    expect(tierToUsd("Tier1032", true)).toBe(2.99);
    expect(tierToUsd("Tier7", false)).toBe(5.99);
    expect(tierToUsd("Free", true)).toBe(0);
    expect(tierToUsd("Base", true)).toBeNull();
    expect(tierToUsd("NotAvailable", false)).toBeNull();
  });
});

/* ── sas ───────────────────────────────────────────────────────────────────────── */

describe("store engine · SAS freshness", () => {
  const now = Date.parse("2026-01-01T12:00:00Z");
  it("fresh / expired / margin boundary", () => {
    expect(isSasUrlFresh("https://b/x?se=2026-01-01T13%3A00%3A00Z", now)).toBe(true);
    expect(isSasUrlFresh("https://b/x?se=2026-01-01T11%3A00%3A00Z", now)).toBe(false);
    // exactly 5 minutes left → NOT fresh (must be strictly beyond the margin)
    expect(isSasUrlFresh("https://b/x?se=2026-01-01T12%3A05%3A00Z", now)).toBe(false);
    expect(isSasUrlFresh("https://b/x?se=2026-01-01T12%3A05%3A01Z", now)).toBe(true);
  });
  it("no query / no se → usable; empty / unparseable → stale", () => {
    expect(isSasUrlFresh("https://b/x", now)).toBe(true);
    expect(isSasUrlFresh("https://b/x?sv=1&sig=y", now)).toBe(true);
    expect(isSasUrlFresh("", now)).toBe(false);
    expect(isSasUrlFresh(undefined, now)).toBe(false);
    expect(isSasUrlFresh("https://b/x?se=not-a-date", now)).toBe(false);
    expect(isSasUrlFresh("::::", now)).toBe(false);
  });
  it("assumes UTC when the offset is missing", () => {
    expect(isSasUrlFresh("https://b/x?se=2026-01-01T13:00:00", now)).toBe(true);
  });
});

/* ── retry ─────────────────────────────────────────────────────────────────────── */

describe("store engine · retry + client", () => {
  it("computeRetryDelay: Retry-After header, body countdown, backoff progression, caps", () => {
    expect(computeRetryDelay(429, 1, "165")).toBe(168); // server countdown + 3s buffer
    expect(computeRetryDelay(429, 1, null, "Try again in 42 seconds")).toBe(45);
    expect(computeRetryDelay(429, 1)).toBe(11); // 2^3 + 3
    expect(computeRetryDelay(500, 1)).toBe(11);
    expect(computeRetryDelay(500, 5)).toBe(131); // 2^7 + 3
    expect(computeRetryDelay(500, 12)).toBe(303); // capped 300 + 3
    expect(computeRetryDelay(429, 1, "500")).toBe(330); // hard cap 5.5 min
  });

  it("classifyHttpError hints", () => {
    expect(classifyHttpError(403)).toMatch(/seller account/);
    expect(classifyHttpError(404)).toMatch(/not found/);
    expect(classifyHttpError(409)).toMatch(/already in progress/);
    expect(classifyHttpError(429, 5)).toMatch(/exhausted 5 retries/);
    expect(classifyHttpError(500)).toBe("");
  });

  it("client succeeds after two 429s and honours maxRetries:0", async () => {
    let calls = 0;
    const { io } = testIO([
      (m, url) => {
        if (url === api("applications/APP")) {
          calls++;
          return calls <= 2 ? { status: 429, body: "Try again in 1 second" } : { status: 200, body: { ok: true } };
        }
        return undefined;
      },
    ]);
    const client = createClient("tok", io);
    await expect(client.request("GET", "applications/APP")).resolves.toEqual({ ok: true });
    expect(calls).toBe(3);

    calls = 0;
    await expect(client.request("GET", "applications/APP", undefined, { maxRetries: 0 })).rejects.toThrow(/HTTP 429/);
    expect(calls).toBe(1);
  });

  it("non-transient errors throw immediately with the API detail", async () => {
    const { io } = testIO([(_m, url) => (url.includes("applications/NOPE") ? { status: 404, body: { code: "NotFound" } } : undefined)]);
    const client = createClient("tok", io);
    await expect(client.request("GET", "applications/NOPE")).rejects.toThrow(/HTTP 404.*not found/s);
  });

  it("token errors are sanitized — the client secret never appears", async () => {
    const secret = "SENTINEL-SECRET-VALUE-12345";
    const { io } = testIO([
      () => ({ status: 400, body: { error: "invalid_client", error_description: "AADSTS7000215: Invalid client secret provided." } }),
    ]);
    try {
      await acquireToken({ tenantId: "t", clientId: "c", clientSecret: secret, source: "env" }, io);
      expect.unreachable("should throw");
    } catch (e) {
      expect((e as Error).message).toMatch(/invalid_client/);
      expect((e as Error).message).not.toContain(secret);
    }
  });
});

/* ── submission shapers ────────────────────────────────────────────────────────── */

describe("store engine · submission shaping", () => {
  it("decidePendingAction matrix", () => {
    expect(decidePendingAction("Certification", false)).toBe("block");
    expect(decidePendingAction("CommitStarted", false)).toBe("block");
    expect(decidePendingAction("Certification", true)).toBe("delete");
    expect(decidePendingAction("CommitFailed", false)).toBe("inspect"); // non-active draft → reuse path
    expect(decidePendingAction("PendingCommit", false)).toBe("inspect");
    expect(decidePendingAction("CommitFailed", true)).toBe("delete");
  });

  it("swapPackages: committed → PendingDelete, uncommitted → dropped", () => {
    const sub: ApiObject = {
      applicationPackages: [
        { id: "p1", fileName: "old.msixupload", fileStatus: "None" },
        { fileName: "stale-uncommitted.msixupload", fileStatus: "PendingUpload" },
      ],
    };
    swapPackages(sub, "new.msixupload");
    const pkgs = sub.applicationPackages as { id?: string; fileName: string; fileStatus: string }[];
    expect(pkgs).toHaveLength(2);
    expect(pkgs[0]).toMatchObject({ id: "p1", fileStatus: "PendingDelete" });
    expect(pkgs[1]).toMatchObject({ fileName: "new.msixupload", fileStatus: "PendingUpload" });
  });

  it("applyScreenshots: committed → PendingDelete, uncommitted → dropped, logos untouched", () => {
    const sub: ApiObject = {
      listings: {
        "en-us": {
          baseListing: {
            images: [
              { id: "i1", imageType: "Screenshot", fileName: "a.png", fileStatus: "None" },
              { imageType: "Screenshot", fileName: "uncommitted.png", fileStatus: "PendingUpload" },
              { id: "i3", imageType: "Logo", fileName: "logo.png", fileStatus: "None" },
            ],
          },
        },
      },
    };
    applyScreenshots(sub, ["s1.png", "s2.png"]);
    const images = ((sub.listings as ApiObject)["en-us"] as { baseListing: { images: ApiObject[] } }).baseListing.images;
    expect(images).toHaveLength(4); // committed screenshot + logo + 2 new
    expect(images[0]).toMatchObject({ id: "i1", fileStatus: "PendingDelete" });
    expect(images[1]).toMatchObject({ imageType: "Logo", fileStatus: "None" });
    expect(images[2]).toMatchObject({ fileName: "s1.png", fileStatus: "PendingUpload", imageType: "Screenshot", description: "s1" });
    // empty list = leave images unchanged
    const before = JSON.stringify(sub);
    applyScreenshots(sub, []);
    expect(JSON.stringify(sub)).toBe(before);
  });

  it("applyListing maps metadata fields incl. caps (20 features / 7 keywords)", () => {
    const sub: ApiObject = {};
    applyListing(sub, {
      description: "d",
      features: Array.from({ length: 25 }, (_, i) => `f${i}`),
      keywords: Array.from({ length: 10 }, (_, i) => `k${i}`),
      notes: { whatsNew: "new stuff" },
      privacyPolicyUrl: "https://p",
      supportUrl: "https://s",
      devStudio: "Acme",
    });
    const base = ((sub.listings as ApiObject)["en-us"] as { baseListing: ApiObject }).baseListing;
    expect(base.description).toBe("d");
    expect((base.features as string[]).length).toBe(20);
    expect((base.keywords as string[]).length).toBe(7);
    expect(base.releaseNotes).toBe("new stuff");
    expect(base.privacyPolicy).toBe("https://p");
    expect(base.supportContact).toBe("https://s");
    expect(base.devStudio).toBe("Acme");
  });

  it("ensureCertNotes: metadata wins, config template substitutes displayName, absence warns", () => {
    const sub: ApiObject = {};
    expect(ensureCertNotes(sub, "Notes for ${displayName}.", { displayName: "Atlas" }).applied).toBe(true);
    expect(sub.notesForCertification).toBe("Notes for Atlas.");
    const sub2: ApiObject = {};
    ensureCertNotes(sub2, "config notes", { notesForCertification: "metadata notes" });
    expect(sub2.notesForCertification).toBe("metadata notes");
    const sub3: ApiObject = {};
    const r = ensureCertNotes(sub3, undefined, undefined);
    expect(r.applied).toBe(false);
    expect(r.warning).toMatch(/no certification notes configured/);
    // existing notes on the cloned submission → no warning
    const sub4: ApiObject = { notesForCertification: "carried over" };
    expect(ensureCertNotes(sub4, undefined, undefined).warning).toBeUndefined();
  });

  it("ensureCategory: only when NotSet, config-driven, warns when unconfigured (no baked-in default)", () => {
    const keep: ApiObject = { applicationCategory: "DeveloperTools" };
    expect(ensureCategory(keep, "Other", undefined)).toEqual({});
    expect(keep.applicationCategory).toBe("DeveloperTools");

    const set: ApiObject = { applicationCategory: "NotSet" };
    expect(ensureCategory(set, "DeveloperTools", undefined).applied).toBe("DeveloperTools");

    const metaWins: ApiObject = {};
    expect(ensureCategory(metaWins, "FromConfig", { applicationCategory: "FromMetadata" }).applied).toBe("FromMetadata");

    const warn = ensureCategory({}, undefined, undefined);
    expect(warn.warning).toMatch(/no category is configured/);
  });

  it("ensureDeviceFamilies: Desktop-only init, never Team, no-op when already set", () => {
    const sub: ApiObject = {};
    expect(ensureDeviceFamilies(sub)).toBe(true);
    expect(sub.allowTargetFutureDeviceFamilies).toEqual({ Desktop: true, Mobile: false, Xbox: false, Holographic: false });
    expect("Team" in (sub.allowTargetFutureDeviceFamilies as ApiObject)).toBe(false);
    const carried: ApiObject = { allowTargetFutureDeviceFamilies: { Desktop: true, Mobile: false, Xbox: false, Holographic: false, Team: false } };
    expect(ensureDeviceFamilies(carried)).toBe(false);
  });

  it("ensureFirstPublishPricing forces Free + NoFreeTrial, preserves markets", () => {
    const sub: ApiObject = { pricing: { priceId: "Tier2", marketSpecificPricings: { DE: {} }, isAdvancedPricingModel: false } };
    ensureFirstPublishPricing(sub);
    expect(sub.pricing).toMatchObject({ priceId: "Free", trialPeriod: "NoFreeTrial", isAdvancedPricingModel: false });
    expect((sub.pricing as ApiObject).marketSpecificPricings).toEqual({ DE: {} });
  });

  it("assertFirstPublishHasListing throws an actionable, product-neutral message", () => {
    expect(() => assertFirstPublishHasListing({})).toThrow(/FIRST submission.*store\.config\.json/s);
    expect(() => assertFirstPublishHasListing({ listings: { "en-us": {} } })).not.toThrow();
  });
});

/* ── addon shapers ─────────────────────────────────────────────────────────────── */

describe("store engine · add-on", () => {
  it("derives the productId from the identity's last dot-segment", () => {
    expect(deriveAddonProductId("AcmeSoft.Atlas")).toBe("AtlasProUpgrade");
    expect(deriveAddonProductId("Solo")).toBe("SoloProUpgrade");
  });

  it("extracts app ids from both association shapes", () => {
    expect(extractAddonAppIds({ applicationIds: ["A", "B"] })).toEqual(["A", "B"]);
    expect(extractAddonAppIds({ applications: { value: [{ id: "C" }] } })).toEqual(["C"]);
    expect(extractAddonAppIds({ applications: [{ id: "D" }] })).toEqual(["D"]);
    expect(extractAddonAppIds({})).toEqual([]);
  });

  it("shapeAddonSubmission: NotSet fixes, explicit tier, markets mirrored, valid values preserved", () => {
    const sub: ApiObject = { contentType: "NotSet", visibility: "NotSet", lifetime: "NotSet", pricing: { priceId: "Base" } };
    shapeAddonSubmission(sub, { title: "T", description: "D", priceTier: "Tier1032", marketSpecificPricings: { DE: {} }, publishMode: "Manual" });
    expect(sub.contentType).toBe("OnlineDownload");
    expect(sub.visibility).toBe("Public");
    expect(sub.lifetime).toBe("Forever");
    expect((sub.pricing as ApiObject).priceId).toBe("Tier1032");
    expect((sub.pricing as ApiObject).marketSpecificPricings).toEqual({ DE: {} });
    expect(sub.targetPublishMode).toBe("Manual");
    expect((sub.listings as ApiObject)["en-us"]).toEqual({ title: "T", description: "D" });

    const valid: ApiObject = { contentType: "ElectronicSoftwareDownload", visibility: "Private", lifetime: "OneDay" };
    shapeAddonSubmission(valid, { title: "T", description: "D", priceTier: "Tier2", publishMode: "Immediate" });
    expect(valid.contentType).toBe("ElectronicSoftwareDownload");
    expect(valid.visibility).toBe("Private");
    expect(valid.lifetime).toBe("OneDay");
    expect((valid.pricing as ApiObject).marketSpecificPricings).toBeUndefined();
  });
});

/* ── zip + images ──────────────────────────────────────────────────────────────── */

function pngBytes(width: number, height: number): Uint8Array {
  const b = new Uint8Array(33);
  b.set([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 13, 0x49, 0x48, 0x44, 0x52], 0);
  new DataView(b.buffer).setUint32(16, width);
  new DataView(b.buffer).setUint32(20, height);
  return b;
}

function jpegBytes(width: number, height: number): Uint8Array {
  // SOI, APP0 (16 bytes), SOF0 with dimensions.
  const b = new Uint8Array(2 + 18 + 2 + 2 + 6);
  let o = 0;
  b.set([0xff, 0xd8], o); o += 2;
  b.set([0xff, 0xe0, 0x00, 0x10], o); o += 4 + 14; // APP0 len 0x10 = 16 (2 len bytes + 14 payload)
  b.set([0xff, 0xc0, 0x00, 0x11, 0x08], o);
  new DataView(b.buffer).setUint16(o + 5, height);
  new DataView(b.buffer).setUint16(o + 7, width);
  return b;
}

describe("store engine · zip + image probe", () => {
  it("zip round-trips flat leaf names with byte-identical content", () => {
    const pkg = new Uint8Array([1, 2, 3, 4]);
    const img = new Uint8Array([9, 8, 7]);
    const zip = buildSubmissionZip([
      { name: "release/deep/App_1.0.msixupload", data: pkg },
      { name: "shot.png", data: img },
    ]);
    const out = unzipSync(zip);
    expect(Object.keys(out).sort()).toEqual(["App_1.0.msixupload", "shot.png"]);
    expect([...out["App_1.0.msixupload"]!]).toEqual([1, 2, 3, 4]);
    expect([...out["shot.png"]!]).toEqual([9, 8, 7]);
  });

  it("probes PNG and JPEG dimensions; rejects garbage", () => {
    expect(probeImageSize(pngBytes(1920, 1080))).toEqual({ width: 1920, height: 1080, format: "png" });
    expect(probeImageSize(jpegBytes(2560, 1440))).toEqual({ width: 2560, height: 1440, format: "jpeg" });
    expect(probeImageSize(new Uint8Array([0, 1, 2, 3, 4, 5]))).toBeNull();
  });

  it("evaluateScreenshot: below-min skip, over-max oversize, 50MB hard skip, 10MB advisory", () => {
    expect(evaluateScreenshot({ name: "s.png", bytes: 1000, width: 800, height: 600 }).verdict).toBe("skip");
    expect(evaluateScreenshot({ name: "s.png", bytes: 1000, width: 5000, height: 2000 }).verdict).toBe("oversize");
    expect(evaluateScreenshot({ name: "s.png", bytes: 51 * 1024 * 1024, width: 1920, height: 1080 }).verdict).toBe("skip");
    const advisory = evaluateScreenshot({ name: "s.png", bytes: 11 * 1024 * 1024, width: 1920, height: 1080 });
    expect(advisory.verdict).toBe("ok");
    expect(advisory.warnings[0]).toMatch(/consider keeping screenshots under 10 MB/);
    expect(evaluateScreenshot({ name: "s.png", bytes: 1000, width: 1920, height: 1080 }).verdict).toBe("ok");
  });
});

/* ── analytics / status / listing ──────────────────────────────────────────────── */

describe("store engine · analytics + status + listing", () => {
  it("summarizeRevenue: gross/tax/revBase/estNet + month bucket from the same rows", () => {
    const appRows = [
      { date: "2026-06-15", acquisitionQuantity: 5 }, // free downloads
      { date: "2026-07-01", acquisitionQuantity: 3, purchasePriceUSDAmount: 30, purchaseTaxUSDAmount: 3 },
    ];
    const addonRows = [
      { date: "2026-06-20", acquisitionQuantity: 2, purchasePriceUSDAmount: 10, purchaseTaxUSDAmount: 1 },
      { date: "2026-07-02", acquisitionQuantity: 1, purchasePriceUSDAmount: 5, purchaseTaxUSDAmount: 0.5 },
    ];
    const s = summarizeRevenue(appRows, addonRows, "2026-07-01", 0.15);
    expect(s.downloads).toBe(8);
    expect(s.units).toBe(6); // 3 paid app + 3 addon
    expect(s.gross).toBe(45);
    expect(s.tax).toBe(4.5);
    expect(s.revBase).toBe(40.5);
    expect(s.estNet).toBeCloseTo(34.42, 2); // 40.5 × 0.85, rounded to cents
    expect(s.month).toMatchObject({ downloads: 3, units: 4, gross: 35, revBase: 31.5 });
  });

  it("mergeAnalyticsCache keeps last-known-good per-app values and recomputes totals", () => {
    const prev = mergeAnalyticsCache(undefined, {
      a: { name: "A", downloads: 100, sales: 5, gross: 50, net: 42, mDownloads: 10, mSales: 1, mGross: 5, mNet: 4 },
      b: { name: "B", downloads: 200, sales: 2, gross: 20, net: 17, mDownloads: 20, mSales: 0, mGross: 0, mNet: 0 },
    }, 0.15, 1);
    // app b failed this run → only a refreshes; b keeps last-known-good
    const next = mergeAnalyticsCache(prev, { a: { name: "A", downloads: 110, sales: 6, gross: 60, net: 51, mDownloads: 11, mSales: 2, mGross: 15, mNet: 13 } }, 0.15, 2);
    expect(next.perApp.b?.downloads).toBe(200);
    expect(next.totals.downloads).toBe(310);
    expect(next.month.downloads).toBe(31);
    expect(next.generatedAt).toBe(2);
  });

  it("buildStatusCache merges per-app rows over the previous cache", () => {
    const row = (id: string, state: StatusRow["state"]): StatusRow => ({
      id, name: id, appId: id.toUpperCase(), state, status: "", published: state === "Published (live)", needsAddon: false, hasAddon: false, errors: [],
    });
    const prev = buildStatusCache([row("a", "Published (live)"), row("b", "In progress")], undefined, 1);
    const next = buildStatusCache([row("b", "Published (live)")], prev, 2);
    expect(next.perApp.a?.state).toBe("Published (live)");
    expect(next.perApp.b?.published).toBe(true);
  });

  it("mergeMetadata: local wins unless empty; force pulls everything", () => {
    const live = {
      appId: "X", primaryName: "Atlas", description: "live desc", shortDescription: "live short",
      features: ["f1"], keywords: ["k1"], websiteUrl: "https://live", privacyPolicyUrl: undefined,
      supportUrl: undefined, copyrightInfo: undefined, releaseNotes: "live notes", screenshots: [],
    };
    const local = { displayName: "Local Atlas", description: "", features: [], notes: { whatsNew: "" } };
    const merged = mergeMetadata(local, live, false);
    expect(merged.merged.displayName).toBe("Local Atlas"); // local wins
    expect(merged.merged.description).toBe("live desc"); // empty → filled
    expect(merged.merged.features).toEqual(["f1"]);
    expect(merged.merged.notes?.whatsNew).toBe("live notes");
    expect(merged.changes).toContain("description");
    expect(merged.changes).not.toContain("displayName");

    const forced = mergeMetadata(local, live, true);
    expect(forced.merged.displayName).toBe("Atlas");
  });
});

/* ── orchestrators (scripted mock fetch) ───────────────────────────────────────── */

const APP = "9NTESTAPP001";

function appRoutes(state: {
  firstPublish?: boolean;
  pending?: { id: string; status: string };
  statusSequence: string[];
  onPut?: (body: string) => void;
  commitStatus?: number;
}): Route[] {
  let statusIdx = 0;
  return [
    (m, url) => {
      if (m === "GET" && url === api(`applications/${APP}`)) {
        return {
          status: 200,
          body: {
            primaryName: "Atlas",
            ...(state.firstPublish ? {} : { lastPublishedApplicationSubmission: { id: "pub-1" } }),
            ...(state.pending ? { pendingApplicationSubmission: { id: state.pending.id } } : {}),
          },
        };
      }
      if (m === "GET" && state.pending && url === api(`applications/${APP}/submissions/${state.pending.id}/status`)) {
        return { status: 200, body: { status: state.pending.status } };
      }
      if (m === "POST" && url === api(`applications/${APP}/submissions`)) {
        return { status: 200, body: { id: "sub-9", fileUploadUrl: freshSas, applicationPackages: [{ id: "p1", fileName: "old.msixupload", fileStatus: "None" }] } };
      }
      if (m === "PUT" && url === api(`applications/${APP}/submissions/sub-9`)) {
        state.onPut?.("");
        return { status: 200, body: {} };
      }
      if (m === "PUT" && url.startsWith("https://blob.example/")) return { status: 201, body: "" };
      if (m === "POST" && url === api(`applications/${APP}/submissions/sub-9/commit`)) {
        return { status: state.commitStatus ?? 200, body: { status: "CommitStarted" } };
      }
      if (m === "GET" && url === api(`applications/${APP}/submissions/sub-9/status`)) {
        const s = state.statusSequence[Math.min(statusIdx++, state.statusSequence.length - 1)]!;
        if (s === "__NETERR__") throw new Error("socket hang up");
        return { status: 200, body: { status: s } };
      }
      if (m === "GET" && url === api(`applications/${APP}/submissions/sub-9`)) {
        return { status: 200, body: { statusDetails: { errors: [{ code: "InvalidState", details: "validation errors" }] } } };
      }
      return undefined;
    },
  ];
}

describe("store engine · submitApp orchestrator", () => {
  const plan = {
    appId: APP,
    packageName: "new.msixupload",
    packageData: new Uint8Array([1]),
    publishMode: "Manual" as const,
    category: "DeveloperTools",
    certNotes: "notes",
    dryRun: false,
    replacePending: false,
    pollTimeoutMs: 10 * 60_000,
    pollIntervalMs: 1,
  };

  it("happy path: update → accepted once the commit leaves the commit phase", async () => {
    const { io } = testIO(appRoutes({ statusSequence: ["CommitStarted", "PreProcessing"] }));
    const client = createClient("tok", io);
    const r = await submitApp(client, io, { ...plan, metadata: { description: "d", displayName: "Atlas" } });
    expect(r.outcome).toBe("accepted");
    expect(r.status).toBe("PreProcessing");
    expect(r.firstPublish).toBe(false);
  });

  it("dry run stops after upload, never commits", async () => {
    let committed = false;
    const routes = appRoutes({ statusSequence: [] });
    const guard: Route = (m, url) => {
      if (m === "POST" && url.endsWith("/commit")) {
        committed = true;
        return { status: 500, body: "should not happen" };
      }
      return undefined;
    };
    const { io } = testIO([guard, ...routes]);
    const client = createClient("tok", io);
    const r = await submitApp(client, io, { ...plan, dryRun: true });
    expect(r.outcome).toBe("dry-run");
    expect(committed).toBe(false);
  });

  it("first publish + CommitFailed → 'prepared' with the privacy/IARC checklist, not a throw", async () => {
    const { io } = testIO(appRoutes({ firstPublish: true, statusSequence: ["CommitFailed"] }));
    const client = createClient("tok", io);
    const r = await submitApp(client, io, { ...plan, metadata: { description: "d" } });
    expect(r.outcome).toBe("prepared");
    expect(r.checklist?.join("\n")).toMatch(/Privacy policy URL/);
    expect(r.checklist?.join("\n")).toMatch(/IARC/);
  });

  it("first publish package-only (no listing) → actionable throw", async () => {
    const { io } = testIO(appRoutes({ firstPublish: true, statusSequence: [] }));
    const client = createClient("tok", io);
    await expect(submitApp(client, io, plan)).rejects.toThrow(/FIRST submission/);
  });

  it("blocks on an actively-processing pending submission without replace consent", async () => {
    const { io } = testIO(appRoutes({ pending: { id: "pend-1", status: "Certification" }, statusSequence: [] }));
    const client = createClient("tok", io);
    const r = await submitApp(client, io, plan);
    expect(r.outcome).toBe("blocked");
    expect(r.checklist?.join(" ")).toMatch(/ONE submission per app/);
  });

  it("poll tolerates a transient network error and continues to acceptance", async () => {
    const { io } = testIO(appRoutes({ statusSequence: ["__NETERR__", "Certification"] }));
    const client = createClient("tok", io);
    const r = await submitApp(client, io, plan);
    expect(r.outcome).toBe("accepted");
  });

  it("update failure (not first publish) throws with the Store's error details", async () => {
    const { io } = testIO(appRoutes({ statusSequence: ["CertificationFailed"] }));
    const client = createClient("tok", io);
    await expect(submitApp(client, io, plan)).rejects.toThrow(/CertificationFailed.*InvalidState/s);
  });
});

describe("store engine · ensureAddon orchestrator", () => {
  function addonRoutes(state: { commitFails?: boolean; statusAfterCommitError?: string; statusSequence: string[] }): Route[] {
    let statusIdx = 0;
    let commitCalls = 0;
    return [
      (m, url) => {
        if (m === "GET" && url === api(`applications/${APP}`)) {
          return { status: 200, body: { primaryName: "Atlas", lastPublishedApplicationSubmission: { id: "pub-1" } } };
        }
        if (m === "GET" && url === api(`applications/${APP}/submissions/pub-1`)) {
          return { status: 200, body: { pricing: { isAdvancedPricingModel: true, marketSpecificPricings: { DE: {} } } } };
        }
        if (m === "GET" && url === api("inappproducts")) {
          return { status: 200, body: { value: [{ id: "9PADDON00001", productId: "AtlasProUpgrade", applicationIds: [APP] }] } };
        }
        if (m === "GET" && url === api("inappproducts/9PADDON00001")) {
          return { status: 200, body: { id: "9PADDON00001" } };
        }
        if (m === "POST" && url === api("inappproducts/9PADDON00001/submissions")) {
          return { status: 200, body: { id: "asub-1", contentType: "NotSet" } };
        }
        if (m === "PUT" && url === api("inappproducts/9PADDON00001/submissions/asub-1")) return { status: 200, body: {} };
        if (m === "POST" && url === api("inappproducts/9PADDON00001/submissions/asub-1/commit")) {
          commitCalls++;
          if (state.commitFails) return { status: 504, body: "gateway timeout" };
          return { status: 200, body: { status: "CommitStarted" } };
        }
        if (m === "GET" && url === api("inappproducts/9PADDON00001/submissions/asub-1")) {
          return { status: 200, body: { statusDetails: { errors: [{ code: "InvalidState", details: "active validation errors which cannot be exposed via API" }] } } };
        }
        if (m === "GET" && url === api("inappproducts/9PADDON00001/submissions/asub-1/status")) {
          if (state.statusAfterCommitError && commitCalls > 0 && statusIdx === 0) {
            statusIdx++;
            return { status: 200, body: { status: state.statusAfterCommitError } };
          }
          const s = state.statusSequence[Math.min(statusIdx++, state.statusSequence.length - 1)] ?? "Certification";
          return { status: 200, body: { status: s } };
        }
        return undefined;
      },
    ];
  }

  const plan = {
    appId: APP,
    identity: "AcmeSoft.Atlas",
    configuredStoreId: "9PADDON00001",
    priceUsd: 4.99,
    appName: "Atlas",
    publishMode: "Manual" as const,
    dryRun: false,
    pollTimeoutMs: 5 * 60_000,
    pollIntervalMs: 1,
  };

  it("resolves the add-on, prices from the APP's advanced model, and accepts", async () => {
    const { io, log } = testIO(addonRoutes({ statusSequence: ["CommitStarted", "Certification"] }));
    const client = createClient("tok", io);
    const r = await ensureAddon(client, io, plan);
    expect(r.outcome).toBe("accepted");
    expect(r.priceTier).toBe("Tier1052"); // $4.99 advanced (from the app submission, not the addon)
    expect(log.join("\n")).toMatch(/markets mirrored from app/);
  });

  it("gateway-failed commit that actually landed → status re-check accepts", async () => {
    const { io } = testIO(addonRoutes({ commitFails: true, statusAfterCommitError: "CommitStarted", statusSequence: ["Certification"] }));
    const client = createClient("tok", io);
    const r = await ensureAddon(client, io, { ...plan, pollTimeoutMs: 60_000 });
    expect(r.outcome).toBe("accepted");
  });

  it("refuses to commit a paid add-on at a guessed price tier (out-of-band price)", async () => {
    const { io } = testIO(addonRoutes({ statusSequence: ["Certification"] }));
    const client = createClient("tok", io);
    // $19.99 has no exact advanced tier (anchors cap at $9.99); committing would mis-price it.
    await expect(ensureAddon(client, io, { ...plan, priceUsd: 19.99 })).rejects.toThrow(
      /mis-price|no exact Store price tier/i,
    );
  });

  it("InvalidState failure → 'prepared' with the one-time base-price checklist", async () => {
    const { io } = testIO(addonRoutes({ statusSequence: ["CommitFailed"] }));
    const client = createClient("tok", io);
    const r = await ensureAddon(client, io, plan);
    expect(r.outcome).toBe("prepared");
    expect(r.checklist?.join("\n")).toMatch(/BASE PRICE\/CURRENCY must be set ONCE/);
  });

  it("skips cleanly when the parent app was never published", async () => {
    const { io } = testIO([
      (m, url) => (m === "GET" && url === api(`applications/${APP}`) ? { status: 200, body: { primaryName: "Atlas" } } : undefined),
    ]);
    const client = createClient("tok", io);
    const r = await ensureAddon(client, io, plan);
    expect(r.outcome).toBe("skipped");
    expect(r.checklist?.join(" ")).toMatch(/once the app is LIVE/);
  });
});

/* ── config ────────────────────────────────────────────────────────────────────── */

describe("store plugin · store.config.json", () => {
  function tempConfig(content: unknown): string {
    const root = mkdtempSync(join(tmpdir(), "navig-storecfg-"));
    writeFileSync(join(root, "store.config.json"), typeof content === "string" ? content : JSON.stringify(content, null, 2), "utf8");
    return root;
  }

  it("loads a valid config; actionable errors for bad JSON / schema / duplicate ids", () => {
    const ok = tempConfig({ version: 1, apps: [{ id: "a", name: "A", appId: "9N1", package: "rel/*.msixupload" }] });
    try {
      const r = loadStoreConfig(ok);
      expect(r.ok).toBe(true);
    } finally {
      rmSync(ok, { recursive: true, force: true });
    }

    const badJson = tempConfig("{nope");
    try {
      const r = loadStoreConfig(badJson);
      expect(r.ok).toBe(false);
      if (!r.ok) expect(r.error).toMatch(/not valid JSON/);
    } finally {
      rmSync(badJson, { recursive: true, force: true });
    }

    const badSchema = tempConfig({ version: 1, apps: [{ id: "a" }] });
    try {
      const r = loadStoreConfig(badSchema);
      expect(r.ok).toBe(false);
      if (!r.ok) expect(r.error).toMatch(/apps\.0/);
    } finally {
      rmSync(badSchema, { recursive: true, force: true });
    }

    const dupe = tempConfig({ version: 1, apps: [
      { id: "a", name: "A", appId: "9N1", package: "x" },
      { id: "a", name: "B", appId: "9N2", package: "y" },
    ] });
    try {
      const r = loadStoreConfig(dupe);
      expect(r.ok).toBe(false);
      if (!r.ok) expect(r.error).toMatch(/duplicate app id/);
    } finally {
      rmSync(dupe, { recursive: true, force: true });
    }
  });

  it("resolvePackagePath: newest glob match wins; actionable misses", () => {
    const root = mkdtempSync(join(tmpdir(), "navig-pkg-"));
    try {
      mkdirSync(join(root, "release"), { recursive: true });
      writeFileSync(join(root, "release", "App_1.0.msixupload"), "old");
      writeFileSync(join(root, "release", "App_2.0.msixupload"), "new");
      utimesSync(join(root, "release", "App_1.0.msixupload"), new Date(2020, 0, 1), new Date(2020, 0, 1));
      utimesSync(join(root, "release", "App_2.0.msixupload"), new Date(2026, 0, 1), new Date(2026, 0, 1));
      const r = resolvePackagePath(root, "release/*.msixupload");
      expect("path" in r && r.path.endsWith("App_2.0.msixupload")).toBe(true);

      const miss = resolvePackagePath(root, "release/*.appxupload");
      expect("error" in miss && /build the package first/.test(miss.error)).toBe(true);

      const noDir = resolvePackagePath(root, "nowhere/*.msixupload");
      expect("error" in noDir && /directory not found/.test(noDir.error)).toBe(true);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("writeBackAddonStoreId updates only the target app's addon", () => {
    const root = tempConfig({
      version: 1,
      apps: [
        { id: "a", name: "A", appId: "9N1", package: "x", addon: { priceUsd: 1.99, storeId: "" } },
        { id: "b", name: "B", appId: "9N2", package: "y", addon: { priceUsd: 2.99, storeId: "9POLD" } },
      ],
    });
    try {
      expect(writeBackAddonStoreId(root, "a", "9PNEW")).toBe(true);
      const after = JSON.parse(readFileSync(join(root, "store.config.json"), "utf8"));
      expect(after.apps[0].addon.storeId).toBe("9PNEW");
      expect(after.apps[1].addon.storeId).toBe("9POLD");
      expect(writeBackAddonStoreId(root, "nope", "9P")).toBe(false);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

/* ── plugin surface + universality ─────────────────────────────────────────────── */

describe("store plugin · universality", () => {
  it("multi-app project: full pipeline actions present, claims + banner keep working", () => {
    const model = modelFor(fx("store-project"));
    const ids = model.allScripts.map((a) => a.id);
    for (const id of ["store.publish", "store.prices", "store.addon", "store.listing", "store.stats", "store.status", "store.creds"]) {
      expect(ids).toContain(id);
    }
    expect(model.allScripts.find((a) => a.id === "store:publish")?.group).toBe("Store"); // claims intact
    expect(model.bannerLines?.some((l) => /Atlas/.test(l.text))).toBe(true); // legacy cache fallback intact
  });

  it("single-app non-screensaver project (tauri-like) activates with the full pipeline", () => {
    const model = modelFor(fx("store-single-app"));
    const store = model.loadedPlugins?.find((p) => p.id === "store");
    expect(store?.active).toBe(true);
    expect(store?.auto).toBe(true);
    expect(model.allScripts.some((a) => a.id === "store.publish")).toBe(true);
  });

  it("no product-specific tokens leak into any model", () => {
    for (const fixture of ["store-project", "store-single-app"]) {
      const json = JSON.stringify(modelFor(fx(fixture)));
      expect(json).not.toMatch(/screensaver/i);
      expect(json).not.toMatch(/cybesis/i);
    }
  });

  it("no config → skeleton-level actions only, no crash", () => {
    const root = mkdtempSync(join(tmpdir(), "navig-nostore-"));
    try {
      writeFileSync(join(root, "package.json"), JSON.stringify({ name: "s", scripts: { "store:publish": "x" } }), "utf8");
      const model = modelFor(root);
      const ids = model.allScripts.map((a) => a.id);
      expect(ids).toContain("store.stats");
      expect(ids).toContain("store.status");
      expect(ids).not.toContain("store.publish"); // pipeline actions need store.config.json
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("empty apps[] config degrades without crashing the model", () => {
    const root = mkdtempSync(join(tmpdir(), "navig-emptystore-"));
    try {
      writeFileSync(join(root, "package.json"), JSON.stringify({ name: "s", scripts: { dev: "vite" } }), "utf8");
      writeFileSync(join(root, "store.config.json"), JSON.stringify({ version: 1, apps: [] }), "utf8");
      const model = modelFor(root);
      expect(model.loadedPlugins?.find((p) => p.id === "store")?.active).toBe(true);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("secret safety: creds-file sentinel values never reach the model", () => {
    const root = mkdtempSync(join(tmpdir(), "navig-secret-"));
    const sentinel = "SENTINEL-TENANT-98765";
    try {
      writeFileSync(join(root, "package.json"), JSON.stringify({ name: "s", scripts: { "store:publish": "x" } }), "utf8");
      writeFileSync(join(root, ".store-creds.json"), JSON.stringify({ tenantId: sentinel, clientId: sentinel, clientSecret: sentinel }), "utf8");
      writeFileSync(join(root, "store.config.json"), JSON.stringify({ version: 1, credsFile: ".store-creds.json", apps: [{ id: "a", name: "A", appId: "9N1", package: "x/*.msixupload" }] }), "utf8");
      const json = JSON.stringify(modelFor(root));
      expect(json).not.toContain(sentinel);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

/* ── headless plugin-action run (cmdRun regression) ────────────────────────────── */

describe("cmdRun · plugin internal actions", () => {
  it("runs a safe plugin handler headlessly with exit 0; unknown target exits 2", async () => {
    const { cmdRun } = await import("../src/commands/index.js");
    const root = mkdtempSync(join(tmpdir(), "navig-cmdrun-"));
    try {
      writeFileSync(join(root, "package.json"), JSON.stringify({ name: "s", scripts: { "store:publish": "x" } }), "utf8");
      const opts = { cwd: root, json: false, deep: false, plain: true, yes: false, relay: false, ai: false, noCache: true };
      // store.status without config/creds takes the cache-age fallback — no prompt, no network.
      expect(await cmdRun(opts, "store.status")).toBe(0);
      expect(await cmdRun(opts, "no.such.action")).toBe(2);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

/* ── contribute-phase cache scoping (framework regression) ─────────────────────── */

describe("plugin cache scoping", () => {
  it("a handler writeCache is visible to the next contribute readCache (same plugin dir)", () => {
    const root = mkdtempSync(join(tmpdir(), "navig-cachescope-"));
    try {
      writeFileSync(join(root, "package.json"), JSON.stringify({ name: "s", scripts: { "store:publish": "x" } }), "utf8");
      const manifest = scanProject(root);
      const actx = makeActionContext({
        root,
        manifest,
        pluginId: "store",
        settings: {},
        theme: createTheme({ plain: true }),
        notify: () => {},
      });
      actx.writeCache("stats", {
        generatedAt: 123,
        storeCut: 0.15,
        totals: { downloads: 4321, sales: 2, gross: 10, net: 8.5 },
        month: { downloads: 1, sales: 0, gross: 0, net: 0 },
        perApp: { one: { name: "Cachetown", downloads: 4321, sales: 2, gross: 10, net: 8.5, mDownloads: 1, mSales: 0, mGross: 0, mNet: 0 } },
      });
      const model = modelFor(root);
      // number formatting is locale-dependent ("4,321" / "4 321") — match digits loosely
      expect(model.bannerLines?.some((l) => /Cachetown/.test(l.text) && /4.?321 downloads/.test(l.text))).toBe(true);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

describe("store engine · init scaffolding", () => {
  it("slugifies display names into kebab config ids", () => {
    expect(slugify("Aurora Borealis Screensaver")).toBe("aurora-borealis-screensaver");
    expect(slugify("WebPocket")).toBe("web-pocket");
    expect(slugify("ChatLens AI")).toBe("chat-lens-ai");
    expect(slugify("   ")).toBe("app");
  });

  it("detects a Tauri app → identity + msix package glob, reading storeProductId as appId", () => {
    const read = (rel: string): string | undefined => {
      if (rel === "src-tauri/tauri.conf.json") return JSON.stringify({ productName: "Snapthos", identifier: "com.cybesis.snapthos" });
      if (rel === "package.json") return JSON.stringify({ name: "snapthos", storeProductId: "9MTPG3W1KCZQ" });
      return undefined;
    };
    const d = detectAppDefaults(read);
    expect(d.kind).toBe("tauri");
    expect(d.name).toBe("Snapthos");
    expect(d.appId).toBe("9MTPG3W1KCZQ");
    expect(d.identity).toBe("CybesisStudios.Snapthos");
    expect(d.packageGlob).toContain("src-tauri/target/release/bundle/msix");
  });

  it("detects a .NET/MSIX app from an AppxManifest identity + display name", () => {
    const read = (rel: string): string | undefined =>
      rel === "AppxManifest.xml"
        ? '<Package><Identity Name="CybesisStudios.AuroraScreensaver" Publisher="CN=x" /><Properties><DisplayName>Aurora</DisplayName></Properties></Package>'
        : undefined;
    const d = detectAppDefaults(read);
    expect(d.kind).toBe("msix-dotnet");
    expect(d.identity).toBe("CybesisStudios.AuroraScreensaver");
    expect(d.name).toBe("Aurora");
  });

  it("accepts an optional per-app build command in the config schema", () => {
    const cfg = { version: 1, apps: [{ id: "a", name: "A", appId: "9N1", package: "rel/*.msix", build: "store:release" }] };
    const parsed = StoreConfigSchema.safeParse(cfg);
    expect(parsed.success).toBe(true);
    if (parsed.success) expect(parsed.data.apps[0]!.build).toBe("store:release");
  });

  it("emits the appId placeholder when none is found and produces a schema-valid config", () => {
    const read = (rel: string): string | undefined => (rel === "package.json" ? JSON.stringify({ name: "ghostapp-cleaner", productName: "GhostApp Cleaner" }) : undefined);
    const d = detectAppDefaults(read);
    expect(d.appId).toBeUndefined();
    const config = buildScaffoldConfig(d, { credsFile: "scripts/.store-creds.json", category: "Utilities" });
    expect(config.apps[0]!.appId).toBe(APPID_PLACEHOLDER);
    expect(config.credsFile).toBe("scripts/.store-creds.json");
    expect(StoreConfigSchema.safeParse(config).success).toBe(true);
  });
});

describe("store engine · WACK", () => {
  it("prefers a bundle, then a package, and never the .msixupload/.appxupload zip", () => {
    expect(pickWackTarget(["app_1.0_x64.msixupload", "app_1.0_x64.msixbundle", "app_1.0_x64.msix"])).toBe("app_1.0_x64.msixbundle");
    expect(pickWackTarget(["app.msixupload", "app.msix"])).toBe("app.msix");
    expect(pickWackTarget(["app.msixupload", "app.appxupload"])).toBeUndefined();
    expect(pickWackTarget([])).toBeUndefined();
  });

  it("parses an appcert report — overall verdict + FAIL/WARNING test names, attribute-order-tolerant", () => {
    const xml = `<REPORT OVERALL_RESULT="FAIL"><TESTS>
      <TEST NAME="Package sanity" RESULT="PASS" />
      <TEST NAME="Supported API test" RESULT="FAIL" />
      <TEST RESULT="WARNING" NAME="Debug configuration test" />
    </TESTS></REPORT>`;
    const v = parseWackReport(xml);
    expect(v.overall).toBe("FAIL");
    expect(v.failed).toEqual(["Supported API test"]);
    expect(v.warnings).toEqual(["Debug configuration test"]);
  });

  it("returns UNKNOWN when the report carries no overall result", () => {
    expect(parseWackReport("<xml></xml>").overall).toBe("UNKNOWN");
  });
});

describe("store engine · creds resolution (.env)", () => {
  it("reads AZURE_* from a root .env when the environment is empty", () => {
    const root = mkdtempSync(join(tmpdir(), "navig-creds-"));
    try {
      writeFileSync(join(root, ".env"), '# creds\nAZURE_TENANT_ID=ten-1\nAZURE_CLIENT_ID="cli-2"\nAZURE_CLIENT_SECRET=sec-3\nUNRELATED=ignored\n', "utf8");
      const r = resolveCreds(root, undefined, {} as NodeJS.ProcessEnv);
      expect(r.ok).toBe(true);
      if (r.ok) {
        expect(r.creds.tenantId).toBe("ten-1");
        expect(r.creds.clientId).toBe("cli-2"); // quotes stripped
        expect(r.creds.clientSecret).toBe("sec-3");
        expect(r.creds.source).toBe("env");
      }
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("lets an exported env var win over the .env file", () => {
    const root = mkdtempSync(join(tmpdir(), "navig-creds-"));
    try {
      writeFileSync(join(root, ".env"), "AZURE_TENANT_ID=from-dotenv\nAZURE_CLIENT_ID=from-dotenv\nAZURE_CLIENT_SECRET=from-dotenv\n", "utf8");
      const r = resolveCreds(root, undefined, { AZURE_TENANT_ID: "from-env" } as NodeJS.ProcessEnv);
      expect(r.ok).toBe(true);
      if (r.ok) {
        expect(r.creds.tenantId).toBe("from-env"); // real env wins
        expect(r.creds.clientId).toBe("from-dotenv"); // gap filled from .env
      }
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("reports the missing NAMES (never values) when nothing resolves", () => {
    const root = mkdtempSync(join(tmpdir(), "navig-creds-"));
    try {
      const r = resolveCreds(root, undefined, {} as NodeJS.ProcessEnv);
      expect(r.ok).toBe(false);
      if (!r.ok) {
        expect(r.missing).toEqual(["AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"]);
        expect(r.hint).toContain(".env");
      }
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});
