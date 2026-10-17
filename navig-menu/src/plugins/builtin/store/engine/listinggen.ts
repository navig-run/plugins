/**
 * AI store-listing generation — the PURE half (prompt building + response parsing + normalization).
 * No network, no filesystem, no AI transport here, so it is fully unit-testable. The handler wires
 * this to an AI provider (env key via runners/ai, or the local `claude` CLI) and to disk.
 *
 * The model's job is COPY, never claims: the system prompt forbids invented features/metrics, and
 * `normalizeListing` hard-clamps every field to the Store's limits so a chatty model can't produce
 * an over-length or malformed listing.
 */

import type { StoreMetadata } from "./types.js";

export interface ListingGenContext {
  /** App display name (authoritative — the model never renames it). */
  name: string;
  identity?: string;
  category?: string;
  /** Coarse stack hints for tone (e.g. ["Windows", "Tauri"]). */
  stack: string[];
  /** Assembled project facts (README excerpt, description, cert notes) — the ONLY source of truth. */
  context: string;
  year: number;
  studio?: string;
}

const LISTING_SYSTEM = [
  "You are a senior Microsoft Store listing copywriter for Windows desktop apps.",
  "Write the store listing for the given app. Output ONLY minified JSON — no prose, no markdown fences:",
  '{"description":"...","shortDescription":"...","features":["..."],"keywords":["..."],"notes":{"whatsNew":"..."},"copyrightInfo":"...","devStudio":"..."}',
  "description: 3-6 short PLAIN-TEXT paragraphs separated by blank lines, benefit-first and concrete — no markdown, no headings, no emoji.",
  "shortDescription: one compelling sentence, at most 200 characters.",
  "features: 5-12 short benefit phrases (each <= 100 chars).",
  "keywords: 5-7 lowercase search terms a user would actually type.",
  "notes.whatsNew: 1-3 short lines suitable for a first release.",
  "HONESTY: base everything STRICTLY on the provided project context. Never invent features, metrics, awards, reviews, or claims the context does not support. JSON only.",
].join(" ");

export function buildListingSystem(): string {
  return LISTING_SYSTEM;
}

export function buildListingUser(ctx: ListingGenContext): string {
  return [
    `App name: ${ctx.name}`,
    ctx.identity ? `MSIX identity: ${ctx.identity}` : "",
    ctx.category ? `Store category: ${ctx.category}` : "",
    `Stack: ${ctx.stack.filter(Boolean).join(", ") || "Windows desktop app"}`,
    "",
    "Project context — use ONLY this, do not invent anything beyond it:",
    ctx.context,
  ]
    .filter(Boolean)
    .join("\n");
}

/** The keyless fallback: a self-contained prompt to paste into any AI. */
export function buildListingPrompt(ctx: ListingGenContext): string {
  return `${LISTING_SYSTEM}\n\n${buildListingUser(ctx)}`;
}

/** Lenient JSON extraction — pulls the first {...} block out of a model reply. */
export function parseListing(text: string): Record<string, unknown> | null {
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start === -1 || end <= start) return null;
  try {
    const v = JSON.parse(text.slice(start, end + 1));
    return v && typeof v === "object" ? (v as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

function str(v: unknown, max: number): string | undefined {
  if (typeof v !== "string") return undefined;
  const t = v.trim();
  return t ? t.slice(0, max) : undefined;
}

function arr(v: unknown, maxItems: number, maxLen: number): string[] | undefined {
  if (!Array.isArray(v)) return undefined;
  const out = v
    .map((x) => (typeof x === "string" ? x.trim() : ""))
    .filter(Boolean)
    .map((x) => x.slice(0, maxLen))
    .slice(0, maxItems);
  return out.length ? out : undefined;
}

export interface NormalizeOpts {
  displayName: string;
  category?: string;
  year: number;
  studio?: string;
}

/**
 * Map + hard-clamp a raw model object into a valid {@link StoreMetadata}. `displayName` and
 * `applicationCategory` are set from trusted config (never the model); everything else is clamped
 * to the Store's field limits so a verbose reply can't produce an invalid listing.
 */
export function normalizeListing(raw: Record<string, unknown>, opts: NormalizeOpts): StoreMetadata {
  const notesRaw = raw.notes && typeof raw.notes === "object" ? (raw.notes as Record<string, unknown>) : {};

  const meta: StoreMetadata = { displayName: opts.displayName };

  const description = str(raw.description, 10000);
  if (description) meta.description = description;
  const shortDescription = str(raw.shortDescription, 200);
  if (shortDescription) meta.shortDescription = shortDescription;
  const features = arr(raw.features, 20, 200);
  if (features) meta.features = features;
  const keywords = arr(raw.keywords, 7, 45);
  if (keywords) meta.keywords = keywords;

  const whatsNew = str(notesRaw.whatsNew, 1500);
  if (whatsNew) meta.notes = { whatsNew };

  const copyright = str(raw.copyrightInfo, 200) ?? (opts.studio ? `(c) ${opts.year} ${opts.studio}` : undefined);
  if (copyright) meta.copyrightInfo = copyright;

  const dev = str(raw.devStudio, 100) ?? opts.studio;
  if (dev) meta.devStudio = dev;

  if (opts.category) meta.applicationCategory = opts.category;

  return meta;
}
