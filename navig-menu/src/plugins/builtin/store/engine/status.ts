/**
 * Submission status (read-only): where each app stands — in certification, published, failed —
 * plus the certification report link. Results merge into a per-app cache so the banner can show
 * live Store state offline.
 */

import type { StoreClient } from "./client.js";
import type { ApiObject, StatusDetails } from "./types.js";

export interface StatusRow {
  id: string;
  name: string;
  appId: string;
  state: "Published (live)" | "In progress" | "FAILED" | "No submission yet" | "ERROR" | "";
  status: string;
  published: boolean;
  needsAddon: boolean;
  hasAddon: boolean;
  certReport?: string;
  lastPublished?: string;
  errors: string[];
}

/** One add-on listing → which app Store IDs already have at least one in-app product. */
export async function collectAddonsByApp(client: StoreClient): Promise<Record<string, boolean>> {
  const map: Record<string, boolean> = {};
  const list = await client.request<{ value?: ApiObject[] }>("GET", "inappproducts");
  for (const iap of list.value ?? []) {
    const ids: string[] = [];
    if (Array.isArray(iap.applicationIds)) ids.push(...(iap.applicationIds as string[]));
    const apps = iap.applications as { value?: { id?: string }[] } | undefined;
    if (apps?.value) ids.push(...apps.value.map((a) => a.id ?? "").filter(Boolean));
    for (const id of ids) map[id] = true;
  }
  return map;
}

export async function fetchAppStatus(
  client: StoreClient,
  target: { id: string; name: string; appId: string; needsAddon: boolean },
  addonByApp: Record<string, boolean>,
): Promise<StatusRow> {
  const row: StatusRow = {
    id: target.id,
    name: target.name,
    appId: target.appId,
    state: "",
    status: "",
    published: false,
    needsAddon: target.needsAddon,
    hasAddon: Boolean(addonByApp[target.appId]),
    errors: [],
  };
  try {
    const app = await client.request<ApiObject>("GET", `applications/${target.appId}`);
    const lastPub = app.lastPublishedApplicationSubmission as { id?: string } | undefined;
    row.published = Boolean(lastPub);
    if (lastPub?.id) row.lastPublished = lastPub.id;

    const pending = app.pendingApplicationSubmission as { id?: string } | undefined;
    if (pending?.id) {
      const st = await client.request<{ status?: string; statusDetails?: StatusDetails }>(
        "GET",
        `applications/${target.appId}/submissions/${pending.id}/status`,
      );
      row.status = st.status ?? "";
      row.state = "In progress";
      if (st.statusDetails?.certificationReports?.length) row.certReport = st.statusDetails.certificationReports[0]?.reportUrl;
      for (const e of st.statusDetails?.errors ?? []) row.errors.push(`ERROR: ${e.code} ${e.details}`);
      for (const w of st.statusDetails?.warnings ?? []) row.errors.push(`WARN: ${w.code} ${w.details}`);
      if (/Failed|Canceled/.test(row.status)) row.state = "FAILED";
    } else if (lastPub) {
      row.state = "Published (live)";
      row.status = "Published";
    } else {
      row.state = "No submission yet";
    }
  } catch (e) {
    row.state = "ERROR";
    row.errors.push((e as Error).message.split("\n")[0] ?? "unknown error");
  }
  return row;
}

export interface StatusCache {
  generatedAt: number;
  perApp: Record<string, { published: boolean; hasAddon: boolean; needsAddon: boolean; state: string; status: string }>;
}

/** Pure: merge fresh rows over the previous cache so a per-app run never wipes the others. */
export function buildStatusCache(rows: StatusRow[], prev: StatusCache | undefined, nowMs: number): StatusCache {
  const perApp = { ...(prev?.perApp ?? {}) };
  for (const r of rows) {
    perApp[r.id] = { published: r.published, hasAddon: r.hasAddon, needsAddon: r.needsAddon, state: r.state, status: r.status };
  }
  return { generatedAt: nowMs, perApp };
}
