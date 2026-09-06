/**
 * Live listing pull + local-metadata merge. Reads the last-published submission (falling back
 * to a pending one) and maps its en-us baseListing to the local metadata field names.
 * Screenshot BYTES are not downloadable through this API — only filenames/count are exposed.
 */

import type { StoreClient } from "./client.js";
import type { ApiObject, StoreMetadata, SubmissionImage } from "./types.js";

export interface ListingSnapshot {
  appId: string;
  primaryName?: string;
  description?: string;
  shortDescription?: string;
  features: string[];
  keywords: string[];
  websiteUrl?: string;
  privacyPolicyUrl?: string;
  supportUrl?: string;
  copyrightInfo?: string;
  releaseNotes?: string;
  screenshots: { fileName?: string; description?: string }[];
  pricing?: ApiObject;
}

export async function fetchListing(client: StoreClient, appId: string): Promise<ListingSnapshot> {
  const app = await client.request<ApiObject>("GET", `applications/${appId}`);
  let subRef = app.lastPublishedApplicationSubmission as { id?: string } | undefined;
  if (!subRef?.id) subRef = app.pendingApplicationSubmission as { id?: string } | undefined;
  if (!subRef?.id) throw new Error(`no submission found for app ${appId}`);

  const sub = await client.request<ApiObject>("GET", `applications/${appId}/submissions/${subRef.id}`);
  const listings = sub.listings as Record<string, { baseListing?: ApiObject }> | undefined;
  const base = listings?.["en-us"]?.baseListing;

  const screenshots: { fileName?: string; description?: string }[] = [];
  for (const im of (base?.images as SubmissionImage[] | undefined) ?? []) {
    if (im.imageType === "Screenshot") screenshots.push({ fileName: im.fileName, description: im.description });
  }

  return {
    appId,
    primaryName: app.primaryName as string | undefined,
    description: base?.description as string | undefined,
    shortDescription: base?.shortDescription as string | undefined,
    features: (base?.features as string[] | undefined) ?? [],
    keywords: (base?.keywords as string[] | undefined) ?? [],
    websiteUrl: base?.websiteUrl as string | undefined,
    privacyPolicyUrl: base?.privacyPolicy as string | undefined,
    supportUrl: base?.supportContact as string | undefined,
    copyrightInfo: base?.copyrightAndTrademarkInfo as string | undefined,
    releaseNotes: base?.releaseNotes as string | undefined,
    screenshots,
    pricing: sub.pricing as ApiObject | undefined,
  };
}

/**
 * Pure: merge a live listing into local metadata. Default: Store values fill only empty/missing
 * local fields (local wins — protects unsubmitted local edits). With `force`, Store wins all
 * mapped fields. Returns the merged object and the list of changed field names.
 */
export function mergeMetadata(local: StoreMetadata, live: ListingSnapshot, force: boolean): { merged: StoreMetadata; changes: string[] } {
  const merged: StoreMetadata = { ...local, notes: { ...(local.notes ?? {}) } };
  const changes: string[] = [];

  const fields: [keyof StoreMetadata & string, unknown][] = [
    ["displayName", live.primaryName],
    ["shortDescription", live.shortDescription],
    ["description", live.description],
    ["websiteUrl", live.websiteUrl],
    ["privacyPolicyUrl", live.privacyPolicyUrl],
    ["supportUrl", live.supportUrl],
    ["copyrightInfo", live.copyrightInfo],
    ["features", live.features.length ? live.features : undefined],
    ["keywords", live.keywords.length ? live.keywords : undefined],
  ];
  for (const [name, liveVal] of fields) {
    if (liveVal === undefined || liveVal === null) continue; // Store has nothing — keep local
    const cur = merged[name];
    const isEmpty =
      cur === undefined || cur === null || (typeof cur === "string" && cur.trim() === "") || (Array.isArray(cur) && cur.length === 0);
    if (!force && !isEmpty) continue; // local has a value; preserve it
    if (JSON.stringify(cur) !== JSON.stringify(liveVal)) {
      (merged as Record<string, unknown>)[name] = liveVal;
      changes.push(name);
    }
  }

  if (live.releaseNotes !== undefined && live.releaseNotes !== null) {
    const cur = merged.notes?.whatsNew;
    const isEmpty = cur === undefined || cur === null || (typeof cur === "string" && cur.trim() === "");
    if ((force || isEmpty) && cur !== live.releaseNotes) {
      merged.notes = { ...(merged.notes ?? {}), whatsNew: live.releaseNotes };
      changes.push("notes.whatsNew");
    }
  }

  return { merged, changes };
}
