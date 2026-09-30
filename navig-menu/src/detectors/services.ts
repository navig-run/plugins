import type { DetectContext } from "./context.js";
import { hasDep } from "./context.js";
import type { Service, Evidence } from "../manifest/schema.js";

/**
 * Services are integrations a menu should expose controls for. We surface evidence and a
 * confidence — we NEVER read or print secret values, only filenames / dependency names.
 */
export function detectServices(ctx: DetectContext): Service[] {
  const out: Service[] = [];
  const files = ctx.scan.files.map((f) => f.rel);

  // Stripe: require the SDK AND (ideally) a webhook route for higher confidence.
  if (hasDep(ctx, "stripe", "@stripe/")) {
    const evidence: Evidence[] = [{ kind: "dep", detail: "stripe" }];
    const webhook = files.find((f) => /stripe/i.test(f) && /(webhook|route)/i.test(f));
    if (webhook) evidence.push({ kind: "route", file: webhook });
    out.push({ id: "stripe", confidence: webhook ? "detected" : "inferred", evidence });
  }

  // Cloudflare: wrangler config or dep.
  const wrangler = files.find((f) => /(^|\/)wrangler\.(toml|jsonc?|ya?ml)$/.test(f));
  if (wrangler || hasDep(ctx, "wrangler", "@cloudflare/")) {
    out.push({
      id: "cloudflare",
      confidence: wrangler ? "detected" : "inferred",
      evidence: wrangler ? [{ kind: "config", file: wrangler }] : [{ kind: "dep", detail: "wrangler" }],
    });
  }

  // Docker compose.
  const compose = files.find((f) => /(^|\/)docker-compose.*\.ya?ml$/.test(f));
  if (compose || files.some((f) => /(^|\/)Dockerfile$/.test(f))) {
    out.push({
      id: "docker",
      confidence: compose ? "detected" : "inferred",
      evidence: compose
        ? [{ kind: "config", file: compose }]
        : [{ kind: "file", file: "Dockerfile" }],
    });
  }

  // Kubernetes / Helm manifests.
  const kube = files.find((f) => /(^|\/)(kustomization\.ya?ml|Chart\.yaml)$/.test(f) || /(^|\/)k8s\//.test(f));
  if (kube) out.push({ id: "kubernetes", confidence: "detected", evidence: [{ kind: "config", file: kube }] });

  pushFile(out, ctx, "vercel", files, [/^vercel\.json$/]);
  pushFile(out, ctx, "netlify", files, [/^netlify\.toml$/]);
  pushFile(out, ctx, "aws", files, [/^serverless\.ya?ml$/, /^template\.ya?ml$/, /^samconfig\.toml$/]);
  pushFile(out, ctx, "firebase", files, [/^firebase\.json$/]);

  pushDep(out, ctx, "supabase", ["@supabase/supabase-js", "supabase"]);
  pushDep(out, ctx, "firebase", ["firebase", "firebase-admin", "firebase-tools"]);
  pushDep(out, ctx, "prisma", ["prisma", "@prisma/client"]);
  pushDep(out, ctx, "drizzle", ["drizzle-orm", "drizzle-kit"]);
  pushDep(out, ctx, "mongodb", ["mongodb", "mongoose"]);
  pushDep(out, ctx, "mysql", ["mysql", "mysql2"]);
  pushDep(out, ctx, "redis", ["redis", "ioredis"]);
  pushDep(out, ctx, "postgres", ["pg", "postgres"]);
  pushDep(out, ctx, "planetscale", ["@planetscale/database"]);
  pushDep(out, ctx, "turso", ["@libsql/client"]);
  pushDep(out, ctx, "sentry", ["@sentry/node", "@sentry/nextjs", "@sentry/react", "@sentry/"]);

  return dedupeServices(out);
}

function pushFile(out: Service[], ctx: DetectContext, id: string, files: string[], res: RegExp[]): void {
  if (out.some((s) => s.id === id)) return;
  const hit = files.find((f) => res.some((re) => re.test(f)));
  if (hit) out.push({ id, confidence: "detected", evidence: [{ kind: "config", file: hit }] });
}

function dedupeServices(list: Service[]): Service[] {
  const map = new Map<string, Service>();
  for (const s of list) if (!map.has(s.id)) map.set(s.id, s);
  return [...map.values()];
}

function pushDep(out: Service[], ctx: DetectContext, id: string, deps: string[]): void {
  if (hasDep(ctx, ...deps)) {
    const hit = deps.find((d) => ctx.deps[d]);
    out.push({ id, confidence: "detected", evidence: [{ kind: "dep", detail: hit ?? deps[0] }] });
  }
}
