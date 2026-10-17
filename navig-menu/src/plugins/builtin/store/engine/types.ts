/**
 * Shared types for the Microsoft Store (Partner Center) submission engine.
 *
 * Everything in `engine/` is UI-free and side-effect-free except through {@link EngineIO} and
 * {@link StoreClient} — so retry/backoff/poll logic is unit-testable with a mock `fetch` and
 * zero real sleeps. The API payloads are the legacy DevCenter submission API shapes
 * (manage.devcenter.microsoft.com/v1.0/my); they are intentionally loose (`Record<string, unknown>`
 * carriers with the few fields the engine touches typed) because submissions are cloned server
 * objects we must round-trip without dropping unknown fields.
 */

/** Injectable I/O so the engine never touches globals directly. */
export interface EngineIO {
  fetch: typeof globalThis.fetch;
  /** Sleep for N milliseconds. */
  sleep(ms: number): Promise<void>;
  /** Current time in ms since epoch. */
  now(): number;
  log(message: string): void;
  warn(message: string): void;
}

/** Azure AD client credentials. Values must never be cached, logged, or thrown. */
export interface StoreCreds {
  tenantId: string;
  clientId: string;
  clientSecret: string;
  /** Where the values came from — safe to display; the values themselves are not. */
  source: "env" | "file" | "mixed";
}

/** A loose JSON object we round-trip to the API without dropping unknown fields. */
export type ApiObject = Record<string, unknown>;

export interface SubmissionImage extends ApiObject {
  id?: string;
  fileName?: string;
  fileStatus?: string;
  imageType?: string;
  description?: string;
}

export interface SubmissionPackage extends ApiObject {
  id?: string;
  fileName?: string;
  fileStatus?: string;
}

export interface StatusDetails {
  errors?: { code?: string; details?: string }[];
  warnings?: { code?: string; details?: string }[];
  certificationReports?: { reportUrl?: string }[];
}

/** A file staged for the submission upload zip. */
export interface UploadFile {
  /** Leaf name — must match the `fileName` referenced in the submission body. */
  name: string;
  data: Uint8Array;
}

/** Listing metadata file (same field names the legacy pipeline's metadata.json uses). */
export interface StoreMetadata {
  displayName?: string;
  description?: string;
  shortDescription?: string;
  features?: string[];
  keywords?: string[];
  notes?: { whatsNew?: string; releaseNotes?: string };
  copyrightInfo?: string;
  websiteUrl?: string;
  privacyPolicyUrl?: string;
  supportUrl?: string;
  devStudio?: string;
  shortTitle?: string;
  voiceTitle?: string;
  applicationCategory?: string;
  notesForCertification?: string;
  [key: string]: unknown;
}

export type PublishMode = "Manual" | "Immediate" | "SpecificDate";
