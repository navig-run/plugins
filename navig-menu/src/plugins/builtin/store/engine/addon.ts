/**
 * Durable in-app-product (add-on) engine — port of the proven pipeline. Preserved edge cases:
 *
 *  - Parent app must be LIVE before an add-on can certify → clean skip, not a cryptic failure.
 *  - The account's TRUE pricing model + market availability are read from the PUBLISHED APP
 *    submission (a freshly-created add-on submission misreports isAdvancedPricingModel).
 *  - Resolve ladder: list match by productId → match by app association → VERIFY the configured
 *    Store id directly before creating (a fresh product lags the list — eventual consistency —
 *    and creating anyway would orphan a duplicate on every re-run) → create → on list failure,
 *    verify the configured id or bail actionably.
 *  - Submission reuse keeps the one-time Partner Center base-price setup; an active submission
 *    can't be edited → clear error.
 *  - contentType/visibility/lifetime are fixed only when NotSet (valid values preserved).
 *  - Commit retries generously (rate-limit windows), and on a thrown commit re-checks the
 *    status — a 5xx commit may have landed ("commit already registered").
 *  - InvalidState / first-ever publish → the "set the base price once in Partner Center"
 *    checklist; the add-on + submission are left intact for a clean re-run.
 */

import type { StoreClient } from "./client.js";
import type { ApiObject, EngineIO, PublishMode } from "./types.js";
import { FAILED_STATES, COMMIT_IN_PROGRESS, ACTIVE_STATES } from "./submission.js";
import { usdToTier } from "./pricing.js";

/** Convention: identity's last dot-segment + "ProUpgrade" (config's addon.productId wins). */
export function deriveAddonProductId(identity: string): string {
  return identity.split(".").pop()! + "ProUpgrade";
}

/** The app Store IDs an in-app product is associated with (the list shape varies by endpoint). */
export function extractAddonAppIds(item: ApiObject): string[] {
  const ids: string[] = [];
  if (Array.isArray(item.applicationIds)) ids.push(...(item.applicationIds as string[]));
  const apps = item.applications as { value?: { id?: string }[] } | { id?: string }[] | undefined;
  if (apps) {
    if (Array.isArray(apps)) ids.push(...apps.map((a) => a.id ?? "").filter(Boolean));
    else if (Array.isArray(apps.value)) ids.push(...apps.value.map((a) => a.id ?? "").filter(Boolean));
  }
  return ids;
}

/** Pure: shape an add-on submission body (listing, contentType, pricing, visibility, lifetime). */
export function shapeAddonSubmission(
  sub: ApiObject,
  opts: {
    title: string;
    description: string;
    priceTier: string;
    marketSpecificPricings?: ApiObject;
    publishMode: PublishMode;
  },
): void {
  sub.listings = { "en-us": { title: opts.title, description: opts.description } };
  // A new submission defaults contentType to 'NotSet', rejected at submit time. A feature
  // unlock delivers no downloadable content → 'OnlineDownload'. Valid values are preserved.
  if (!sub.contentType || sub.contentType === "NotSet") sub.contentType = "OnlineDownload";
  // To SET a price you must send an explicit tier — priceId="Base" only preserves an existing
  // base price and errors on an add-on that has none.
  const pricing = (sub.pricing as ApiObject | undefined) ?? {};
  pricing.priceId = opts.priceTier;
  if (opts.marketSpecificPricings && Object.keys(opts.marketSpecificPricings).length > 0) {
    pricing.marketSpecificPricings = opts.marketSpecificPricings;
  }
  sub.pricing = pricing;
  // Brand-new API-created add-ons default these to 'NotSet' (accepted on PUT, rejected at commit).
  if (!sub.visibility || sub.visibility === "NotSet") sub.visibility = "Public";
  if (!sub.lifetime || sub.lifetime === "NotSet") sub.lifetime = "Forever";
  sub.targetPublishMode = opts.publishMode;
}

export interface AddonPlan {
  appId: string;
  identity?: string;
  productId?: string;
  /** Configured Store ID of the add-on ("" / undefined = not created yet). */
  configuredStoreId?: string;
  priceUsd: number;
  /** Force an exact tier id, overriding the amount→tier mapping. */
  priceTierOverride?: string;
  title?: string;
  description?: string;
  /** Display name used in default title/description. */
  appName: string;
  publishMode: PublishMode;
  dryRun: boolean;
  pollTimeoutMs: number;
  pollIntervalMs?: number;
}

export interface AddonResult {
  outcome: "committed" | "accepted" | "prepared" | "dry-run" | "skipped" | "blocked";
  storeId?: string;
  productId?: string;
  priceTier?: string;
  /** New Store id the caller should persist into the project config. */
  writeBackStoreId?: string;
  messages: string[];
  checklist?: string[];
}

export async function ensureAddon(client: StoreClient, io: EngineIO, plan: AddonPlan): Promise<AddonResult> {
  const messages: string[] = [];
  const say = (m: string) => {
    messages.push(m);
    io.log(m);
  };

  if (plan.priceUsd <= 0) {
    return { outcome: "skipped", messages: [`no add-on price configured for ${plan.appName} — nothing to do`] };
  }
  const productId = plan.productId ?? (plan.identity ? deriveAddonProductId(plan.identity) : undefined);
  if (!productId) {
    throw new Error(`cannot derive the add-on productId for ${plan.appName} — set addon.productId or identity in store.config.json`);
  }
  const title = plan.title ?? `${plan.appName} Pro Upgrade`;
  const description =
    plan.description ??
    `Unlock all Pro features of ${plan.appName} — every option and preset. One-time purchase, yours forever.`;

  // ---- precondition: parent app must be LIVE ------------------------------------
  let published = false;
  let lastPublishedId: string | undefined;
  try {
    const app = await client.request<ApiObject>("GET", `applications/${plan.appId}`);
    lastPublishedId = (app.lastPublishedApplicationSubmission as { id?: string } | undefined)?.id;
    published = Boolean(lastPublishedId);
    say(`app published before: ${published}`);
  } catch (e) {
    io.warn(`could not read application state: ${(e as Error).message.split("\n")[0]}`);
  }
  if (!published) {
    return {
      outcome: "skipped",
      productId,
      messages,
      checklist: [
        `${plan.appName} has no published submission yet — an add-on can only be created once the app is LIVE`,
        "publish the app first (it must pass certification), then re-run this action",
      ],
    };
  }

  // ---- authoritative pricing model + markets from the published APP submission ---
  let advancedModel = true;
  let marketPricings: ApiObject | undefined;
  try {
    const appSub = await client.request<{ pricing?: { isAdvancedPricingModel?: boolean; marketSpecificPricings?: ApiObject } }>(
      "GET",
      `applications/${plan.appId}/submissions/${lastPublishedId}`,
    );
    if (appSub.pricing) {
      if (appSub.pricing.isAdvancedPricingModel !== undefined && appSub.pricing.isAdvancedPricingModel !== null) {
        advancedModel = Boolean(appSub.pricing.isAdvancedPricingModel);
      }
      marketPricings = appSub.pricing.marketSpecificPricings;
      say(`app pricing: advancedModel=${advancedModel}, market overrides=${Object.keys(marketPricings ?? {}).length}`);
    }
  } catch (e) {
    io.warn(`could not read app submission pricing (using add-on defaults): ${(e as Error).message.split("\n")[0]}`);
  }

  // ---- resolve or create the in-app product --------------------------------------
  let storeId: string | undefined;
  let createdNow = false;
  let writeBackStoreId: string | undefined;
  let listed = false;
  let items: ApiObject[] = [];
  try {
    const list = await client.request<{ value?: ApiObject[] }>("GET", "inappproducts");
    listed = true;
    items = list.value ?? [];
  } catch (e) {
    io.warn(`could not list add-ons: ${(e as Error).message.split("\n")[0]}`);
  }

  const configured = plan.configuredStoreId?.trim() ?? "";
  if (listed) {
    const byProduct = items.find((i) => i.productId === productId);
    const byApp = byProduct ?? items.find((i) => extractAddonAppIds(i).includes(plan.appId));
    if (byApp?.id) {
      storeId = String(byApp.id);
      say(`found add-on '${byApp.productId}' → ${storeId}`);
      if (configured !== storeId) writeBackStoreId = storeId; // self-heal drifted config
    } else {
      // Nothing listed — but that is NOT proof the configured add-on is stale: a fresh product
      // lags the list (eventual consistency). Trust-but-verify before creating a duplicate.
      if (configured) {
        try {
          const verify = await client.request<ApiObject>("GET", `inappproducts/${configured}`);
          if (verify?.id) {
            storeId = String(verify.id);
            say(`configured add-on ${configured} verified directly (not yet in list — propagation lag); reusing`);
          }
        } catch (e) {
          io.warn(`configured add-on id '${configured}' is not accessible (${(e as Error).message.split("\n")[0]}) — recreating`);
        }
      }
      if (!storeId) {
        const iap = await client.request<ApiObject>("POST", "inappproducts", {
          applicationIds: [plan.appId],
          productId,
          productType: "Durable",
        });
        storeId = String(iap.id ?? iap.inAppProductId ?? "");
        if (!storeId) throw new Error(`in-app product create did not return an id (productId: ${productId})`);
        createdNow = true;
        writeBackStoreId = storeId;
        say(`in-app product created: ${storeId}`);
      }
    }
  } else {
    // Listing failed (transient / permission). Don't risk creating a duplicate.
    if (!configured) {
      throw new Error(
        `could not list add-ons and no add-on Store id is configured for ${plan.appName}. Retry once add-on listing works, or create the add-on in Partner Center → Add-ons.`,
      );
    }
    await client.request("GET", `inappproducts/${configured}`); // throws with the API's hint on failure
    storeId = configured;
    say(`using configured add-on ${configured} (list unavailable; id verified)`);
  }

  // ---- reuse or create the submission ---------------------------------------------
  const iapInfo = await client.request<ApiObject>("GET", `inappproducts/${storeId}`);
  let sub: ApiObject | undefined;
  const pendRef = (iapInfo.pendingInAppProductSubmission as { id?: string } | undefined)?.id;
  if (pendRef) {
    let pendStatus = "Unknown";
    try {
      const st = await client.request<{ status?: string }>("GET", `inappproducts/${storeId}/submissions/${pendRef}/status`);
      pendStatus = st.status ?? "Unknown";
    } catch {
      /* treat as non-active */
    }
    if (ACTIVE_STATES.includes(pendStatus)) {
      return {
        outcome: "blocked",
        storeId,
        productId,
        messages,
        checklist: [
          `an add-on submission is already in progress (id ${pendRef}, status: ${pendStatus})`,
          "wait for it to finish, or cancel it in Partner Center → Add-ons, then re-run",
        ],
      };
    }
    sub = await client.request<ApiObject>("GET", `inappproducts/${storeId}/submissions/${pendRef}`);
    say(`reusing existing add-on submission ${pendRef} (status: ${pendStatus}) — keeps Partner Center pricing setup`);
  }
  if (!sub) {
    sub = await client.request<ApiObject>("POST", `inappproducts/${storeId}/submissions`);
    say("created add-on submission");
  }
  const subId = String(sub.id ?? "");
  if (!subId) throw new Error("add-on submission create/read did not return id");

  const tierResult = plan.priceTierOverride
    ? { tier: plan.priceTierOverride, exact: true as const }
    : usdToTier(plan.priceUsd, advancedModel);
  // Refuse to commit a paid add-on at a GUESSED tier — the nearest-anchor fallback caps at $9.99, so
  // proceeding would silently mis-price (e.g. $19.99 → sold at $9.99). Require an exact supported
  // price or an explicit `priceTier` override in store.config.json.
  if (plan.priceUsd > 0 && !tierResult.exact) {
    throw new Error(
      `no exact Store price tier for $${plan.priceUsd.toFixed(2)} (${advancedModel ? "advanced" : "legacy"} scheme) — ` +
        `committing would mis-price the add-on. Use a supported $X.99 price, or set an explicit ` +
        `"priceTier" in store.config.json.` +
        ("note" in tierResult && tierResult.note ? `\n${tierResult.note}` : ""),
    );
  }
  shapeAddonSubmission(sub, {
    title,
    description,
    priceTier: tierResult.tier,
    marketSpecificPricings: marketPricings,
    publishMode: plan.publishMode,
  });
  say(`pricing: priceId=${tierResult.tier} ($${plan.priceUsd.toFixed(2)}, model=${advancedModel ? "advanced" : "legacy"}), markets mirrored from app`);

  await client.request("PUT", `inappproducts/${storeId}/submissions/${subId}`, sub);
  say("add-on submission prepared");

  if (plan.dryRun) {
    say(`dry run: add-on submission ${subId} prepared but NOT committed — review in Partner Center → Add-ons`);
    return { outcome: "dry-run", storeId, productId, priceTier: tierResult.tier, writeBackStoreId, messages };
  }

  // ---- commit (heavily rate-limited endpoint) + poll --------------------------------
  let commitStatus: string | undefined;
  try {
    const commit = await client.request<{ status?: string }>("POST", `inappproducts/${storeId}/submissions/${subId}/commit`, undefined, {
      maxRetries: 8,
    });
    commitStatus = commit.status;
  } catch (e) {
    // A 5xx commit may still have landed server-side — re-check before surfacing the error.
    let st: string | undefined;
    try {
      st = (await client.request<{ status?: string }>("GET", `inappproducts/${storeId}/submissions/${subId}/status`)).status;
    } catch {
      /* keep original error */
    }
    if (st && st !== "PendingCommit") {
      say(`commit already registered (status: ${st}) despite a gateway error — continuing`);
      commitStatus = st;
    } else {
      throw e;
    }
  }
  say(`commit started (status: ${commitStatus ?? "?"})`);

  const deadline = io.now() + plan.pollTimeoutMs;
  const interval = plan.pollIntervalMs ?? 10_000;
  for (;;) {
    await io.sleep(interval);

    // Transient poll failures after an accepted commit must not fail the run — keep polling.
    let status: string;
    try {
      status = (await client.request<{ status?: string }>("GET", `inappproducts/${storeId}/submissions/${subId}/status`)).status ?? "Unknown";
    } catch (e) {
      io.log(`status poll failed (transient — retrying): ${(e as Error).message.split("\n")[0]}`);
      if (io.now() > deadline) {
        throw new Error(`timed out waiting for add-on commit (last poll errored). Check Partner Center.`);
      }
      continue;
    }
    say(`status: ${status}`);

    // A 200 with no `status` is ambiguous, not success — keep polling rather than false-accept.
    if (status === "Unknown") {
      if (io.now() > deadline) {
        throw new Error(`timed out — the add-on status endpoint returned no status. Check Partner Center.`);
      }
      continue;
    }

    if (FAILED_STATES.includes(status)) {
      let errText = "";
      try {
        const details = await client.request<{ statusDetails?: { errors?: { code?: string; details?: string }[]; warnings?: { code?: string; details?: string }[] } }>(
          "GET",
          `inappproducts/${storeId}/submissions/${subId}`,
        );
        errText = [
          ...(details.statusDetails?.errors ?? []).map((e) => `ERROR: ${e.code} - ${e.details}`),
          ...(details.statusDetails?.warnings ?? []).map((w) => `WARN:  ${w.code} - ${w.details}`),
        ].join("\n");
      } catch {
        /* best-effort */
      }
      // A brand-new Durable add-on whose base price/currency was never established in Partner
      // Center fails commit with an opaque "InvalidState" — the API can PUT a tier but can't do
      // that one-time setup. Nothing is lost: the submission is reused on the next run.
      if (createdNow || /InvalidState/i.test(errText)) {
        return {
          outcome: "prepared",
          storeId,
          productId,
          priceTier: tierResult.tier,
          writeBackStoreId,
          messages,
          checklist: [
            `the add-on + submission are created (Store ID: ${storeId}, productId: ${productId}) — nothing is lost`,
            "a brand-new add-on's BASE PRICE/CURRENCY must be set ONCE in Partner Center (the API cannot establish it); after that, this action handles all future price updates",
            `1. Partner Center → your app → Add-ons → '${productId}' → Pricing and availability: https://partner.microsoft.com/dashboard/products/${plan.appId}/add-ons`,
            `2. set the base price to $${plan.priceUsd.toFixed(2)} (tier ${tierResult.tier}), Save, then Submit the add-on once`,
            "3. re-run this action — it reuses the submission and commits cleanly",
            ...(errText ? [`Store reported: ${errText}`] : []),
          ],
        };
      }
      throw new Error(`add-on submission failed with status '${status}'.\n${errText}`);
    }

    if (!COMMIT_IN_PROGRESS.includes(status)) {
      say(`add-on submission accepted — status: ${status} (Store ID: ${storeId}, priceId: ${tierResult.tier} = $${plan.priceUsd.toFixed(2)})`);
      return { outcome: "accepted", storeId, productId, priceTier: tierResult.tier, writeBackStoreId, messages };
    }
    if (io.now() > deadline) {
      throw new Error(`timed out waiting for add-on commit (last status: ${status}). Check Partner Center.`);
    }
  }
}
