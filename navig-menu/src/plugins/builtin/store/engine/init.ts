/**
 * Zero-to-config scaffolding for the store plugin — makes onboarding a brand-new app one action
 * instead of hand-writing `store.config.json`. Pure + side-effect-free (takes a `read` function),
 * so the detection + shaping is unit-testable; the handler does the fs writes and prompts.
 *
 * The Store *product ID* (`appId`) is the one thing the plugin can never detect — it's assigned by
 * Partner Center. Scaffolds emit {@link APPID_PLACEHOLDER} for it so the config is valid-shaped and
 * the user knows exactly the single value to paste in.
 */

import type { StoreConfig, StoreApp } from "../config.js";
import type { StoreMetadata } from "./types.js";

/** Marker for the one human-supplied value (the Partner Center Store product ID). */
export const APPID_PLACEHOLDER = "SET-STORE-PRODUCT-ID";

export type AppKind = "tauri" | "msix-dotnet" | "unknown";

export interface DetectedApp {
  /** kebab slug for the config `id`. */
  id: string;
  /** Display name. */
  name: string;
  /** Store product ID if we can find one (package.json storeProductId / existing metadata), else undefined. */
  appId?: string;
  /** MSIX package identity (from an AppxManifest) or a derived default. */
  identity?: string;
  /** Repo-relative package glob for the built upload artifact. */
  packageGlob: string;
  /** Detected app category, if any. */
  category?: string;
  kind: AppKind;
  /** Where the build artifact comes from — surfaced to the user so they wire a build script. */
  buildHint: string;
}

/** A reader that returns file text (or undefined if absent/unreadable). Injected for testability. */
export type Reader = (rel: string) => string | undefined;

/** kebab-case an app name for the config id (drop non-alphanumerics, collapse dashes). */
export function slugify(name: string): string {
  return (
    name
      .replace(/([a-z0-9])([A-Z])/g, "$1-$2")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "") || "app"
  );
}

/** PascalCase a name for an MSIX identity leaf (Cybesis convention: `Publisher.<Pascal>`). */
function pascal(name: string): string {
  return name.replace(/[^a-zA-Z0-9]+/g, " ").trim().split(/\s+/).map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join("") || "App";
}

function firstMatch(text: string | undefined, re: RegExp): string | undefined {
  if (!text) return undefined;
  const m = re.exec(text);
  return m?.[1]?.trim() || undefined;
}

/**
 * Detect an app's store defaults from whatever build tooling is present. Tauri (src-tauri or
 * tauri-ui/src-tauri) and .NET/MSIX (an AppxManifest) are recognized; anything else falls back to
 * package.json. `publisher` seeds the derived MSIX identity when no manifest declares one.
 */
export function detectAppDefaults(read: Reader, opts?: { publisher?: string }): DetectedApp {
  const publisher = opts?.publisher || "CybesisStudios";
  const pkgRaw = read("package.json");
  let pkg: { name?: string; productName?: string; description?: string; storeProductId?: string } = {};
  try {
    if (pkgRaw) pkg = JSON.parse(pkgRaw);
  } catch {
    /* unreadable package.json — fall through to other signals */
  }

  // Existing listing metadata can carry the appId + display name.
  let metaAppId: string | undefined;
  let metaName: string | undefined;
  for (const rel of ["store/metadata.json", "store/LISTING.md"]) {
    const raw = read(rel);
    if (!raw) continue;
    if (rel.endsWith(".json")) {
      try {
        const m = JSON.parse(raw) as { displayName?: string; appId?: string; storeProductId?: string };
        metaName ||= m.displayName;
        metaAppId ||= m.appId ?? m.storeProductId;
      } catch {
        /* ignore */
      }
    }
  }

  // ── Tauri ──────────────────────────────────────────────────────────────────
  for (const base of ["src-tauri", "tauri-ui/src-tauri", "apps/tauri/src-tauri"]) {
    const confRaw = read(`${base}/tauri.conf.json`);
    if (!confRaw) continue;
    let conf: { productName?: string; identifier?: string } = {};
    try {
      conf = JSON.parse(confRaw);
    } catch {
      /* keep scanning */
    }
    const name = metaName || conf.productName || pkg.productName || pkg.name || "App";
    return {
      id: slugify(name),
      name,
      appId: metaAppId || pkg.storeProductId,
      // Tauri identifiers are reverse-DNS (dev.foo.bar); MSIX identity is PascalPublisher.PascalName.
      identity: `${publisher}.${pascal(name)}`,
      packageGlob: `${base}/target/release/bundle/msix/*_x64.msix`,
      kind: "tauri",
      buildHint: `Tauri MSIX — build with your app's release build (e.g. \`tauri build\`); artifact lands in ${base}/target/release/bundle/msix/`,
    };
  }

  // ── .NET / native MSIX (an AppxManifest) ─────────────────────────────────────
  for (const rel of ["AppxManifest.xml", "src/AppxManifest.xml", "build/AppxManifest.xml", "packaging/AppxManifest.xml"]) {
    const manifest = read(rel);
    if (!manifest) continue;
    const idName = firstMatch(manifest, /<Identity[^>]*\bName="([^"]+)"/);
    const displayName = firstMatch(manifest, /<DisplayName>([^<]+)<\/DisplayName>/) || metaName || pkg.productName || pkg.name || idName || "App";
    return {
      id: slugify(displayName),
      name: displayName,
      appId: metaAppId || pkg.storeProductId,
      identity: idName || `${publisher}.${pascal(displayName)}`,
      packageGlob: "release/*/*.msixupload",
      kind: "msix-dotnet",
      buildHint: "MSIX (MakeAppx/wapproj) — build + pack into an .msixupload under release/<app>/",
    };
  }

  // ── Fallback ─────────────────────────────────────────────────────────────────
  const name = metaName || pkg.productName || pkg.name || "App";
  return {
    id: slugify(name),
    name,
    appId: metaAppId || pkg.storeProductId,
    identity: `${publisher}.${pascal(name)}`,
    packageGlob: "release/*/*.msixupload",
    kind: "unknown",
    buildHint: "no Tauri/AppxManifest detected — set `package` to your built .msixupload/.msix and wire a build script the STORE rail can run",
  };
}

/** Build a valid single-app `store.config.json` object from a detected app. */
export function buildScaffoldConfig(app: DetectedApp, opts?: { credsFile?: string; category?: string; notesForCertification?: string; metadataPath?: string }): StoreConfig {
  const appEntry: StoreApp = {
    id: app.id,
    name: app.name,
    appId: app.appId && app.appId.trim() ? app.appId.trim() : APPID_PLACEHOLDER,
    identity: app.identity,
    package: app.packageGlob,
    metadata: opts?.metadataPath ?? "store/metadata.json",
  };
  if (app.category ?? opts?.category) appEntry.category = app.category ?? opts?.category;
  const config: StoreConfig = { version: 1, apps: [appEntry] };
  if (opts?.credsFile) config.credsFile = opts.credsFile;
  const defaults: NonNullable<StoreConfig["defaults"]> = {};
  if (opts?.category) defaults.category = opts.category;
  if (opts?.notesForCertification) defaults.notesForCertification = opts.notesForCertification;
  if (Object.keys(defaults).length) config.defaults = defaults;
  return config;
}

/** A minimal, honest listing stub — real copy comes from `store.metagen` or a hand edit. */
export function buildMetadataStub(app: DetectedApp): StoreMetadata {
  return {
    displayName: app.name,
    shortDescription: "",
    description: "",
    features: [],
    keywords: [],
    privacyPolicyUrl: "",
    supportUrl: "",
  };
}
