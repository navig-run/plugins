import { z } from "zod";

/**
 * The data model shared across detectors → builder → UI → runner.
 *
 * Two artifacts:
 *  - the generated **manifest** (`.navig/menu.cache.json`, gitignored): pure detection
 *    output with evidence + confidence.
 *  - the human/AI-owned **menu definition** (`.navig/menu.json`, versioned): the override
 *    that wins on merge. Shaped to be easy for an AI to author (SKILL.md-like).
 */

export const ConfidenceSchema = z.enum(["explicit", "detected", "inferred", "unknown"]);
export type Confidence = z.infer<typeof ConfidenceSchema>;

export const RiskSchema = z.enum(["safe", "confirm", "dangerous"]);
export type Risk = z.infer<typeof RiskSchema>;

export const StatusToneSchema = z.enum([
  "success",
  "info",
  "warning",
  "danger",
  "neutral",
  "busy",
  "disabled",
]);
export type StatusTone = z.infer<typeof StatusToneSchema>;

/** Why something was detected. NEVER carries a secret value — only filenames / var names. */
export const EvidenceSchema = z.object({
  kind: z.enum(["field", "lockfile", "file", "dep", "script", "route", "config", "binary"]),
  file: z.string().optional(),
  detail: z.string().optional(),
});
export type Evidence = z.infer<typeof EvidenceSchema>;

export const DetectedSchema = <T extends z.ZodTypeAny>(value: T) =>
  z.object({
    value,
    confidence: ConfidenceSchema,
    evidence: z.array(EvidenceSchema).default([]),
  });

export const PackageManagerSchema = DetectedSchema(
  z.enum(["npm", "pnpm", "yarn", "bun", "none"]),
);
export type PackageManagerInfo = z.infer<typeof PackageManagerSchema>;

export const WorkspaceSchema = z.object({
  kind: z.enum(["pnpm", "npm", "turbo", "nx", "pseudo", "nested", "none"]),
  confidence: ConfidenceSchema,
  packages: z.array(z.string()).default([]),
  evidence: z.array(EvidenceSchema).default([]),
});
export type WorkspaceInfo = z.infer<typeof WorkspaceSchema>;

export const FrameworkSchema = z.object({
  id: z.string(),
  confidence: ConfidenceSchema,
  evidence: z.array(EvidenceSchema).default([]),
});
export type Framework = z.infer<typeof FrameworkSchema>;

export const ServiceSchema = FrameworkSchema;
export type Service = z.infer<typeof ServiceSchema>;

/** A runnable item. `launcher` + `argv` are passed to subprocess as an ARRAY (no shell). */
export const ActionSchema = z.object({
  id: z.string(),
  canonical: z.string().optional(),
  label: z.string(),
  description: z.string().optional(),
  group: z.string().default("Utilities"),
  /** Owning workspace project (for the per-project menu view). Derived; not persisted. */
  project: z.string().optional(),
  launcher: z.string(),
  argv: z.array(z.string()).default([]),
  cwd: z.string().default("."),
  risk: RiskSchema.default("safe"),
  longRunning: z.boolean().default(false),
  confidence: ConfidenceSchema.default("detected"),
  why: z.string().optional(),
  /** Personal pre-run note (logins / important info). Shown before the command runs. */
  note: z.string().optional(),
  evidence: z.array(EvidenceSchema).default([]),
  source: z.string().default("project-scripts"),
});
export type Action = z.infer<typeof ActionSchema>;

export const WarningSchema = z.object({
  code: z.string(),
  detail: z.string(),
});
export type Warning = z.infer<typeof WarningSchema>;

/** A reachable surface for the banner (web app, API, etc.). Detected, never secret. */
export const EndpointSchema = z.object({
  id: z.string(),
  label: z.string(),
  kind: z.enum(["web", "api", "service"]).default("web"),
  url: z.string(),
  tls: z.boolean().optional(),
  evidence: z.array(EvidenceSchema).default([]),
});
export type Endpoint = z.infer<typeof EndpointSchema>;

/** Accent palette keys (theme tints the banner + section headers). */
export const AccentSchema = z.enum([
  "cyan",
  "blue",
  "green",
  "magenta",
  "purple",
  "yellow",
  "red",
]);
export type Accent = z.infer<typeof AccentSchema>;

/** Glyph fidelity: emoji (full), unicode (geometric), or ascii (portable). */
export const GlyphStyleSchema = z.enum(["emoji", "unicode", "ascii"]);
export type GlyphStyle = z.infer<typeof GlyphStyleSchema>;

/** Per-row banner visibility — every row is "only show if it exists" AND enabled here. */
export const BannerUiSchema = z.object({
  spacedTitle: z.boolean().optional(),
  tagline: z.string().optional(),
  endpoints: z.boolean().optional(),
  stack: z.boolean().optional(),
  git: z.boolean().optional(),
  runtime: z.boolean().optional(),
  date: z.boolean().optional(),
  packages: z.boolean().optional(),
  stats: z.boolean().optional(),
  plugins: z.boolean().optional(),
});
export type BannerUi = z.infer<typeof BannerUiSchema>;

/** Per-project plugin activation + settings state (persisted in `.navig/menu.json`). */
export const PluginStateSchema = z.record(
  z.string(),
  z.object({
    enabled: z.boolean().optional(),
    settings: z.record(z.string(), z.union([z.boolean(), z.string()])).optional(),
  }),
);
export type PluginState = z.infer<typeof PluginStateSchema>;

export const MenuUiSchema = z.object({
  recentLimit: z.number().int().min(0).max(20).optional(),
  showDescriptions: z.boolean().optional(),
  showCommandHints: z.boolean().optional(),
  showCounts: z.boolean().optional(),
  showRiskBadges: z.boolean().optional(),
  showRecents: z.boolean().optional(),
  showNavigSection: z.boolean().optional(),
  mouse: z.boolean().optional(),
  glyphs: GlyphStyleSchema.optional(),
  accent: AccentSchema.optional(),
  density: z.enum(["comfortable", "compact"]).optional(),
  layout: z.enum(["flat", "list", "categories", "tree", "projects"]).optional(),
  bannerStyle: z.enum(["cosmic", "console"]).optional(),
  banner: BannerUiSchema.optional(),
  footer: z.string().optional(),
});
export type MenuUi = z.infer<typeof MenuUiSchema>;

export const ManifestSchema = z.object({
  schemaVersion: z.number(),
  generatedAt: z.string().optional(),
  tool: z.object({ name: z.string(), version: z.string() }),
  fingerprint: z.string(),
  root: z.string(),
  name: z.string().optional(),
  version: z.string().optional(),
  license: z.string().optional(),
  purpose: z
    .object({ value: z.string(), confidence: ConfidenceSchema })
    .optional(),
  packageManager: PackageManagerSchema,
  workspace: WorkspaceSchema,
  frameworks: z.array(FrameworkSchema).default([]),
  services: z.array(ServiceSchema).default([]),
  endpoints: z.array(EndpointSchema).default([]),
  docs: z.number().default(0),
  actions: z.array(ActionSchema).default([]),
  scripts: z.record(z.string(), z.string()).default({}),
  warnings: z.array(WarningSchema).default([]),
});
export type Manifest = z.infer<typeof ManifestSchema>;

/* ── Human / AI-owned menu definition (.navig/menu.json) ───────────────────── */

const OverrideActionSchema = z.union([
  z.string(),
  z.object({
    cmd: z.string(),
    label: z.string().optional(),
    description: z.string().optional(),
    risk: RiskSchema.optional(),
    longRunning: z.boolean().optional(),
    group: z.string().optional(),
    why: z.string().optional(),
  }),
]);

export const MenuDefinitionSchema = z.object({
  $schema: z.string().optional(),
  title: z.string().optional(),
  purpose: z.string().optional(),
  accent: z.string().optional(),
  packageManager: z.enum(["npm", "pnpm", "yarn", "bun"]).optional(),
  actions: z.record(z.string(), OverrideActionSchema).optional(),
  hide: z.array(z.string()).optional(),
  /** Relabel / regroup / redescribe any DETECTED script by its id (curate without re-adding). */
  overrides: z
    .record(
      z.string(),
      z.object({
        label: z.string().optional(),
        description: z.string().optional(),
        group: z.string().optional(),
        risk: RiskSchema.optional(),
        longRunning: z.boolean().optional(),
        hide: z.boolean().optional(),
        /** Personal pre-run note shown before this command runs (logins / reminders). */
        note: z.string().optional(),
      }),
    )
    .optional(),
  /** Custom section presentation + order (name → title/glyph/tone). Order here wins. */
  groups: z
    .array(
      z.object({
        name: z.string(),
        title: z.string().optional(),
        icon: z.string().optional(),
        ascii: z.string().optional(),
        tone: z.string().optional(),
      }),
    )
    .optional(),
  extra: z
    .array(
      z.object({
        id: z.string(),
        label: z.string(),
        cmd: z.string(),
        description: z.string().optional(),
        group: z.string().optional(),
        risk: RiskSchema.optional(),
        longRunning: z.boolean().optional(),
        cwd: z.string().optional(),
        why: z.string().optional(),
        note: z.string().optional(),
      }),
    )
    .optional(),
  ui: MenuUiSchema.optional(),
  plugins: PluginStateSchema.optional(),
  defaultHost: z.string().optional(),
});
export type MenuDefinition = z.infer<typeof MenuDefinitionSchema>;
