import { z } from "zod";
import type { MenuPlugin, PluginContext } from "./types.js";
import { RiskSchema } from "../manifest/schema.js";

/**
 * The declarative plugin manifest (`.navig/plugins/*.json` or `.yaml`). Pure data — no code path —
 * so it is safe to load anywhere. `declarativeToPlugin` maps it onto the same `MenuPlugin` the rest
 * of the system consumes, so declarative and programmatic plugins are handled identically.
 */

const ToneSchema = z.enum([
  "accent", "dim", "green", "yellow", "red", "blue", "cyan", "magenta", "purple", "white",
]);

const ActionSpecSchema = z.object({
  id: z.string(),
  label: z.string(),
  cmd: z.string().optional(),
  description: z.string().optional(),
  risk: RiskSchema.optional(),
  longRunning: z.boolean().optional(),
  delegateTo: z.string().optional(),
  internal: z.string().optional(),
});

export const DeclarativePluginSchema = z.object({
  id: z.string(),
  version: z.string().optional(),
  detect: z
    .object({
      scripts: z.string().optional(), // regex over script names
      files: z.array(z.string()).optional(),
      deps: z.array(z.string()).optional(),
    })
    .optional(),
  text: z
    .object({
      title: z.string().optional(),
      tagline: z.string().optional(),
      purpose: z.string().optional(),
      prompt: z.string().optional(),
    })
    .optional(),
  bannerLines: z.array(z.object({ text: z.string(), tone: ToneSchema.optional() })).optional(),
  bannerPhrases: z.array(z.string()).optional(),
  sections: z
    .array(
      z.object({
        group: z.string(),
        meta: z
          .object({
            title: z.string().optional(),
            emoji: z.string().optional(),
            unicode: z.string().optional(),
            ascii: z.string().optional(),
            tone: z.string().optional(),
          })
          .optional(),
        actions: z.array(ActionSpecSchema),
      }),
    )
    .optional(),
  claims: z
    .array(z.object({ match: z.string(), group: z.string(), labelPrefix: z.string().optional(), risk: RiskSchema.optional() }))
    .optional(),
  settings: z
    .array(
      z.object({
        key: z.string(),
        label: z.string(),
        type: z.enum(["toggle", "cycle"]),
        values: z.array(z.string()).optional(),
        default: z.union([z.boolean(), z.string()]),
      }),
    )
    .optional(),
  about: z.array(z.string()).optional(),
});
export type DeclarativePlugin = z.infer<typeof DeclarativePluginSchema>;

export function declarativeToPlugin(decl: DeclarativePlugin): MenuPlugin {
  return {
    id: decl.id,
    tier: "declarative",
    version: decl.version,
    detect: decl.detect ? (ctx: PluginContext) => matchDetect(decl.detect!, ctx) : undefined,
    contribute: () => ({
      text: decl.text,
      bannerLines: decl.bannerLines,
      bannerPhrases: decl.bannerPhrases,
      // Declarative plugins can't run handlers → drop internal-only actions.
      sections: decl.sections?.map((s) => ({ ...s, actions: s.actions.filter((a) => !a.internal) })),
      claims: decl.claims,
      settings: decl.settings,
      about: decl.about,
    }),
  };
}

function matchDetect(d: NonNullable<DeclarativePlugin["detect"]>, ctx: PluginContext): boolean {
  if (d.scripts) {
    try {
      const re = new RegExp(d.scripts);
      if (Object.keys(ctx.scripts).some((k) => re.test(k))) return true;
    } catch {
      /* bad regex → ignore this predicate */
    }
  }
  if (d.files?.some((f) => ctx.hasFile(f))) return true;
  if (d.deps?.length && ctx.hasDep(...d.deps)) return true;
  return false;
}
