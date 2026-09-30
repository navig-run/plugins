/**
 * App submission engine — the TypeScript port of the proven Store pipeline
 * (create/reuse submission → listing + screenshots → package swap → PUT → SAS upload →
 * commit → poll). Preserved edge cases:
 *
 *  - PENDING-DRAFT REUSE: a non-active pending submission is reused (not delete+recreate) so
 *    manual Partner Center work the API can't set — IARC age rating, data-collection
 *    declaration — survives pipeline re-runs. An actively-processing submission is only
 *    replaced with an explicit `replacePending`.
 *  - Stale SAS on a reused draft → recreate fresh.
 *  - Committed packages/screenshots (with an id) → PendingDelete; uncommitted ones (no id,
 *    staged by a prior reused run that never committed) → dropped, since the API rejects
 *    existing entries without an id.
 *  - First publish: pricing forced Free + NoFreeTrial, listing required, category must be set
 *    (from config/metadata — never a baked-in product default), device families initialized
 *    Desktop-only (never Team — the API rejects it), and a CommitFailed maps to the
 *    privacy-policy + IARC checklist with the submission left intact for a re-run.
 */

import { isSasUrlFresh } from "./sas.js";
import { buildSubmissionZip } from "./zip.js";
import { uploadToSas } from "./upload.js";
import type { StoreClient } from "./client.js";
import type { ApiObject, EngineIO, PublishMode, StoreMetadata, SubmissionImage, SubmissionPackage, UploadFile } from "./types.js";

export const ACTIVE_STATES = ["CommitStarted", "PreProcessing", "Certification", "Release", "PendingPublication", "Publishing"];
export const FAILED_STATES = ["CommitFailed", "PreProcessingFailed", "CertificationFailed", "ReleaseFailed", "PublishFailed", "Canceled"];
export const COMMIT_IN_PROGRESS = ["CommitStarted", "PendingCommit"];

export function isActiveStatus(status: string): boolean {
  return ACTIVE_STATES.includes(status);
}

/**
 * Pure: what to do with an existing pending submission.
 *  - "block":   actively processing and no replace consent — refuse (one submission per app).
 *  - "delete":  replace it (explicit consent, or replacing an active one).
 *  - "inspect": non-active draft — fetch it and reuse when its SAS is still fresh.
 */
export function decidePendingAction(status: string, replacePending: boolean): "block" | "delete" | "inspect" {
  if (isActiveStatus(status)) return replacePending ? "delete" : "block";
  if (replacePending) return "delete";
  return "inspect";
}

/* ── pure body shapers ─────────────────────────────────────────────────────────── */

function baseListingOf(submission: ApiObject): ApiObject {
  let listings = submission.listings as ApiObject | undefined;
  if (!listings || typeof listings !== "object") {
    listings = {};
    submission.listings = listings;
  }
  let lang = listings["en-us"] as ApiObject | undefined;
  if (!lang || typeof lang !== "object") {
    lang = {};
    listings["en-us"] = lang;
  }
  let base = lang.baseListing as ApiObject | undefined;
  if (!base || typeof base !== "object") {
    base = {};
    lang.baseListing = base;
  }
  return base;
}

/** Overwrite the en-us baseListing fields from a metadata file (null/absent fields untouched). */
export function applyListing(submission: ApiObject, metadata: StoreMetadata): void {
  const base = baseListingOf(submission);
  const set = (name: string, value: unknown) => {
    if (value === undefined || value === null) return;
    base[name] = value;
  };
  set("description", metadata.description);
  if (metadata.features?.length) set("features", metadata.features.slice(0, 20));
  if (metadata.keywords?.length) set("keywords", metadata.keywords.slice(0, 7));
  if (metadata.notes?.whatsNew) set("releaseNotes", metadata.notes.whatsNew);
  if (metadata.copyrightInfo) set("copyrightAndTrademarkInfo", metadata.copyrightInfo);
  if (metadata.websiteUrl) set("websiteUrl", metadata.websiteUrl);
  if (metadata.privacyPolicyUrl) set("privacyPolicy", metadata.privacyPolicyUrl);
  if (metadata.supportUrl) set("supportContact", metadata.supportUrl);
  if (metadata.shortDescription) set("shortDescription", metadata.shortDescription);
  if (metadata.devStudio) set("devStudio", metadata.devStudio);
  if (metadata.shortTitle) set("shortTitle", metadata.shortTitle);
  if (metadata.voiceTitle) set("voiceTitle", metadata.voiceTitle);
}

/**
 * Replace the listing's Screenshot images: committed ones (id) → PendingDelete, uncommitted
 * ones (no id) → dropped; non-screenshot images (logos) untouched; new files appended as
 * PendingUpload. No-op when `newFileNames` is empty (listing images left unchanged).
 */
export function applyScreenshots(submission: ApiObject, newFileNames: string[]): void {
  if (!newFileNames.length) return;
  const base = baseListingOf(submission);
  const kept: SubmissionImage[] = [];
  const images = (base.images as SubmissionImage[] | undefined) ?? [];
  for (const im of images) {
    if (im.imageType === "Screenshot") {
      if (im.id) {
        im.fileStatus = "PendingDelete";
        kept.push(im);
      }
      // else: uncommitted, no id — drop it (the fresh PendingUpload list replaces it)
    } else {
      kept.push(im);
    }
  }
  const fresh: SubmissionImage[] = newFileNames.map((fileName) => ({
    fileName,
    fileStatus: "PendingUpload",
    imageType: "Screenshot",
    description: fileName.replace(/\.[^.]+$/, ""),
  }));
  base.images = [...kept, ...fresh];
}

/** Swap the package list: committed (id) → PendingDelete, uncommitted (no id) → dropped. */
export function swapPackages(submission: ApiObject, packageFileName: string): void {
  const kept: SubmissionPackage[] = [];
  const pkgs = (submission.applicationPackages as SubmissionPackage[] | undefined) ?? [];
  for (const p of pkgs) {
    if (p.id) {
      p.fileStatus = "PendingDelete";
      kept.push(p);
    }
    // else: uncommitted package with no id — drop it (replaced by the new PendingUpload)
  }
  submission.applicationPackages = [...kept, { fileName: packageFileName, fileStatus: "PendingUpload" }];
}

/**
 * Certification notes come ONLY from config/metadata (metadata.notesForCertification wins).
 * `${displayName}` in the configured template is substituted. Returns a warning when the
 * submission would go out with no notes at all — the engine never injects a product default.
 */
export function ensureCertNotes(
  submission: ApiObject,
  configuredNotes: string | undefined,
  metadata: StoreMetadata | undefined,
): { applied: boolean; warning?: string } {
  const displayName = metadata?.displayName ?? "this app";
  const raw = metadata?.notesForCertification ?? configuredNotes;
  if (raw) {
    submission.notesForCertification = raw.replaceAll("${displayName}", displayName);
    return { applied: true };
  }
  const existing = submission.notesForCertification as string | undefined;
  if (existing && existing.trim()) return { applied: false };
  return {
    applied: false,
    warning:
      "no certification notes configured (notesForCertification in store.config.json defaults/app or metadata.json) — testers may fail the app as 'not testable' if its primary functionality isn't obvious",
  };
}

/**
 * First-time publishes clone nothing, so `applicationCategory` comes back NotSet, which the API
 * rejects on PUT. Set a category ONLY when it's missing/NotSet, from metadata/config — never a
 * baked-in default. Returns a warning when it's needed but unconfigured.
 */
export function ensureCategory(
  submission: ApiObject,
  configuredCategory: string | undefined,
  metadata: StoreMetadata | undefined,
): { applied?: string; warning?: string } {
  const cur = String(submission.applicationCategory ?? "");
  if (cur && cur !== "NotSet") return {};
  const category = metadata?.applicationCategory || configuredCategory;
  if (category) {
    submission.applicationCategory = category;
    return { applied: category };
  }
  return {
    warning:
      "applicationCategory is NotSet and no category is configured — the Store may reject the submission. Set defaults.category (or apps[].category / metadata applicationCategory) in store.config.json.",
  };
}

/**
 * A fresh first-publish submission comes back with allowTargetFutureDeviceFamilies = {} which
 * the API rejects. Initialize as Desktop-only when no platform keys exist. NOT 'Team' — the
 * API rejects it even as false for accounts without that permission.
 */
export function ensureDeviceFamilies(submission: ApiObject): boolean {
  const df = submission.allowTargetFutureDeviceFamilies as ApiObject | undefined;
  if (df && Object.prototype.hasOwnProperty.call(df, "Desktop")) return false;
  submission.allowTargetFutureDeviceFamilies = { Desktop: true, Mobile: false, Xbox: false, Holographic: false };
  return true;
}

/** First publish: force the base app Free with no trial (updates keep their live pricing). */
export function ensureFirstPublishPricing(submission: ApiObject): void {
  const pricing = (submission.pricing as ApiObject | undefined) ?? {};
  pricing.priceId = "Free";
  pricing.trialPeriod = "NoFreeTrial";
  if (!Object.prototype.hasOwnProperty.call(pricing, "isAdvancedPricingModel")) pricing.isAdvancedPricingModel = true;
  submission.pricing = pricing;
}

/** First publish requires at least one Store listing — throw an actionable error, not the raw API one. */
export function assertFirstPublishHasListing(submission: ApiObject): void {
  const listings = submission.listings as ApiObject | undefined;
  const hasListing = listings && Object.keys(listings).length > 0;
  if (!hasListing) {
    throw new Error(
      [
        "This is the app's FIRST submission and it has NO Store listing yet — a package-only upload can't create one.",
        "The Store requires a listing (description + screenshots) on the first submission.",
        "Fix: configure `metadata` and `screenshots` for this app in store.config.json and run a full publish.",
        "Package-only uploads only work for UPDATES, after the app has been published once.",
      ].join("\n"),
    );
  }
}

/* ── orchestrator ─────────────────────────────────────────────────────────────── */

export interface SubmitPlan {
  appId: string;
  /** Leaf file name of the package (must match the name inside the upload zip). */
  packageName: string;
  packageData: Uint8Array;
  metadata?: StoreMetadata;
  /** Validated, upload-ready screenshots. Empty/omitted = leave listing images unchanged. */
  screenshots?: UploadFile[];
  publishMode: PublishMode;
  /** Certification-notes template from store.config.json (metadata wins over it). */
  certNotes?: string;
  /** Category from store.config.json (metadata wins over it). */
  category?: string;
  dryRun: boolean;
  replacePending: boolean;
  pollTimeoutMs: number;
  pollIntervalMs?: number;
}

export interface SubmitResult {
  outcome: "committed" | "accepted" | "prepared" | "dry-run" | "blocked";
  submissionId?: string;
  status?: string;
  firstPublish: boolean;
  messages: string[];
  checklist?: string[];
}

interface AppResource extends ApiObject {
  primaryName?: string;
  pendingApplicationSubmission?: { id?: string };
  lastPublishedApplicationSubmission?: { id?: string };
}

export async function submitApp(client: StoreClient, io: EngineIO, plan: SubmitPlan): Promise<SubmitResult> {
  const messages: string[] = [];
  const say = (m: string) => {
    messages.push(m);
    io.log(m);
  };

  // ---- read app + resolve any pending submission -------------------------------
  const app = await client.request<AppResource>("GET", `applications/${plan.appId}`);
  say(`app: ${app.primaryName ?? plan.appId}`);
  const firstPublish = !app.lastPublishedApplicationSubmission?.id;

  let submission: ApiObject | undefined;
  let reusing = false;
  const pendingId = app.pendingApplicationSubmission?.id;
  if (pendingId) {
    let pendStatus = "Unknown";
    try {
      const st = await client.request<{ status?: string }>("GET", `applications/${plan.appId}/submissions/${pendingId}/status`);
      pendStatus = st.status ?? "Unknown";
    } catch {
      /* status unreadable → treat as Unknown (non-active) */
    }
    const action = decidePendingAction(pendStatus, plan.replacePending);
    if (action === "block") {
      return {
        outcome: "blocked",
        firstPublish,
        messages,
        checklist: [
          `an existing submission is already in progress (id ${pendingId}, status: ${pendStatus})`,
          "the Store allows only ONE submission per app at a time — re-submitting now would CANCEL it and restart certification (the 1-3 business day clock)",
          "wait for it to finish, or re-run and choose to replace the pending submission",
        ],
      };
    }
    if (action === "delete") {
      if (isActiveStatus(pendStatus)) io.warn(`replacing in-progress submission ${pendingId} (status: ${pendStatus}) — its certification will be canceled`);
      await client.request("DELETE", `applications/${plan.appId}/submissions/${pendingId}`);
      say("pending submission cleared");
    } else {
      // Non-active draft: reuse it — preserves manual Partner Center edits (IARC, data declaration).
      const reuse = await client.request<ApiObject>("GET", `applications/${plan.appId}/submissions/${pendingId}`);
      if (typeof reuse.fileUploadUrl === "string" && isSasUrlFresh(reuse.fileUploadUrl, io.now())) {
        submission = reuse;
        reusing = true;
        say(`reusing existing submission ${pendingId} (status: ${pendStatus}) — keeps your manual Partner Center edits`);
      } else {
        io.warn("existing submission has no fresh upload URL — recreating fresh");
        await client.request("DELETE", `applications/${plan.appId}/submissions/${pendingId}`);
      }
    }
  }

  // ---- create (or reuse) --------------------------------------------------------
  if (!submission) {
    submission = await client.request<ApiObject>("POST", `applications/${plan.appId}/submissions`);
    say("created new submission (clones last published)");
  }
  const submissionId = String(submission.id ?? "");
  const fileUploadUrl = String(submission.fileUploadUrl ?? "");
  if (!submissionId || !fileUploadUrl) throw new Error("submission create/read did not return id/fileUploadUrl");

  // ---- listing + screenshots ----------------------------------------------------
  if (plan.metadata) {
    applyListing(submission, plan.metadata);
    say("listing fields set from metadata");
    io.warn("age rating (IARC) is not settable via API — it carries from the cloned submission");
  }
  const screenshotFiles = plan.screenshots ?? [];
  if (screenshotFiles.length) {
    applyScreenshots(
      submission,
      screenshotFiles.map((f) => f.name),
    );
    say(`${screenshotFiles.length} screenshot(s) staged for upload`);
  }

  // ---- package swap + submission-wide fields ------------------------------------
  swapPackages(submission, plan.packageName);
  submission.targetPublishMode = plan.publishMode;

  const notes = ensureCertNotes(submission, plan.certNotes, plan.metadata);
  if (notes.warning) io.warn(notes.warning);
  const cat = ensureCategory(submission, plan.category, plan.metadata);
  if (cat.applied) say(`applicationCategory set to '${cat.applied}'`);
  if (cat.warning) io.warn(cat.warning);
  if (ensureDeviceFamilies(submission)) say("allowTargetFutureDeviceFamilies initialized (Desktop only)");
  if (firstPublish) {
    ensureFirstPublishPricing(submission);
    say("pricing set to Free (first publish — base app, no charge)");
    assertFirstPublishHasListing(submission);
  }

  // ---- PUT + upload --------------------------------------------------------------
  await client.request("PUT", `applications/${plan.appId}/submissions/${submissionId}`, submission);
  say("submission metadata updated");

  const zip = buildSubmissionZip([{ name: plan.packageName, data: plan.packageData }, ...screenshotFiles]);
  say(`zipped ${(zip.length / 1024 / 1024).toFixed(1)} MB (${1 + screenshotFiles.length} file(s)) → uploading…`);
  await uploadToSas(io, fileUploadUrl, zip);
  say("upload complete");

  if (plan.dryRun) {
    say(`dry run: submission ${submissionId} prepared but NOT committed — review it in Partner Center`);
    return { outcome: "dry-run", submissionId, firstPublish, messages };
  }

  // ---- commit + poll --------------------------------------------------------------
  let commitStatus: string | undefined;
  try {
    const commit = await client.request<{ status?: string }>(
      "POST",
      `applications/${plan.appId}/submissions/${submissionId}/commit`,
      undefined,
      { maxRetries: 8 },
    );
    commitStatus = commit.status;
  } catch (e) {
    // A 5xx on commit may still have landed server-side — re-check before surfacing the error, so a
    // transient gateway failure doesn't report a certifying submission as failed (and block re-run).
    let st: string | undefined;
    try {
      st = (await client.request<{ status?: string }>("GET", `applications/${plan.appId}/submissions/${submissionId}/status`)).status;
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
  const interval = plan.pollIntervalMs ?? 15_000;
  for (;;) {
    await io.sleep(interval);

    // Transient poll failures after an accepted commit must not fail the run — keep polling.
    let status: string;
    try {
      const st = await client.request<{ status?: string }>("GET", `applications/${plan.appId}/submissions/${submissionId}/status`);
      status = st.status ?? "Unknown";
    } catch (e) {
      io.log(`status poll failed (transient — retrying): ${(e as Error).message.split("\n")[0]}`);
      if (io.now() > deadline) {
        throw new Error(`timed out waiting for commit (last poll errored). The submission may still complete — check Partner Center.`);
      }
      continue;
    }
    say(`status: ${status}`);

    // A 200 with no `status` field is ambiguous, not success — keep polling (deadline-bounded)
    // rather than falsely reporting "accepted".
    if (status === "Unknown") {
      if (io.now() > deadline) {
        throw new Error(`timed out — the status endpoint returned no status. The submission may still complete — check Partner Center.`);
      }
      continue;
    }

    if (FAILED_STATES.includes(status)) {
      let errText = "";
      try {
        const details = await client.request<{ statusDetails?: { errors?: { code?: string; details?: string }[]; warnings?: { code?: string; details?: string }[] } }>(
          "GET",
          `applications/${plan.appId}/submissions/${submissionId}`,
        );
        const errs = [
          ...(details.statusDetails?.errors ?? []).map((e) => `ERROR: ${e.code} - ${e.details}`),
          ...(details.statusDetails?.warnings ?? []).map((w) => `WARN:  ${w.code} - ${w.details}`),
        ];
        errText = errs.join("\n");
      } catch {
        /* best-effort */
      }
      if (firstPublish) {
        // Almost always the human-gated Partner Center items the API can't set. The package +
        // listing are staged on the submission (reuse preserves them), so guide, don't fail.
        return {
          outcome: "prepared",
          submissionId,
          status,
          firstPublish,
          messages,
          checklist: [
            "package + listing are uploaded — a FIRST publish also needs (the API can't set these):",
            "1. Privacy policy URL (REQUIRED when the package declares internet capabilities — 'no data collected' does NOT waive it)",
            "2. Age ratings (IARC): complete the questionnaire",
            `set them here, then re-run (it reuses the submission + commits): https://partner.microsoft.com/dashboard/products/${plan.appId}/submissions/${submissionId}`,
            ...(errText ? [`Store reported: ${errText}`] : []),
          ],
        };
      }
      throw new Error(`submission failed with status '${status}'.\n${errText}`);
    }

    if (!COMMIT_IN_PROGRESS.includes(status)) {
      say("submission accepted — now in Microsoft's certification pipeline (1-3 business days)");
      say(
        plan.publishMode === "Manual"
          ? "publish mode is Manual: after certification, click 'Publish' in Partner Center to go live"
          : `publish mode is ${plan.publishMode}: it will go live automatically once certified`,
      );
      say("note: Partner Center's new UI may show 'Pricing: Not started' for legacy-API values — the submission still certifies");
      return { outcome: "accepted", submissionId, status, firstPublish, messages };
    }

    if (io.now() > deadline) {
      throw new Error(`timed out waiting for commit (last status: ${status}). The submission may still complete — check Partner Center.`);
    }
  }
}
