/**
 * Azure AD credential resolution: env vars win, then an optional gitignored creds file
 * (`{ "tenantId", "clientId", "clientSecret" }`). SECRET SAFETY: values are resolved only at
 * call time inside handlers, stay function-scoped, and must never reach caches, notify lines,
 * banner text, or error messages — report which NAMES are missing, never what values exist.
 */

import { readFileSync } from "node:fs";
import { join, isAbsolute } from "node:path";
import type { StoreCreds } from "./types.js";

export const CRED_ENV_NAMES = ["AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"] as const;

export type CredsResult = { ok: true; creds: StoreCreds } | { ok: false; missing: string[]; hint: string };

/**
 * Read only the three AZURE_* keys from a project-root `.env` (the preferred, gitignored home for
 * these creds). A tiny KEY=VALUE parser — no `dotenv` dep, never throws, and deliberately ignores
 * every other key so unrelated secrets in `.env` are never even read into memory.
 */
function readDotEnvCreds(root: string): Partial<Record<(typeof CRED_ENV_NAMES)[number], string>> {
  const out: Partial<Record<(typeof CRED_ENV_NAMES)[number], string>> = {};
  let text: string;
  try {
    text = readFileSync(join(root, ".env"), "utf8");
  } catch {
    return out;
  }
  const wanted = new Set<string>(CRED_ENV_NAMES);
  for (const line of text.split(/\r?\n/)) {
    const t = line.trim();
    if (!t || t.startsWith("#")) continue;
    const eq = t.indexOf("=");
    if (eq < 1) continue;
    const key = t.slice(0, eq).trim();
    if (!wanted.has(key)) continue;
    let val = t.slice(eq + 1).trim();
    if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) val = val.slice(1, -1);
    out[key as (typeof CRED_ENV_NAMES)[number]] = val;
  }
  return out;
}

export function resolveCreds(root: string, credsFile?: string, env: NodeJS.ProcessEnv = process.env): CredsResult {
  let tenantId = env.AZURE_TENANT_ID ?? "";
  let clientId = env.AZURE_CLIENT_ID ?? "";
  let clientSecret = env.AZURE_CLIENT_SECRET ?? "";
  const fromEnv = Boolean(tenantId && clientId && clientSecret);

  // Root `.env` (env-shaped, gitignored) — the migration target. Real exported env still wins.
  let usedDotEnv = false;
  if (!tenantId || !clientId || !clientSecret) {
    const dot = readDotEnvCreds(root);
    if (!tenantId && dot.AZURE_TENANT_ID) { tenantId = dot.AZURE_TENANT_ID; usedDotEnv = true; }
    if (!clientId && dot.AZURE_CLIENT_ID) { clientId = dot.AZURE_CLIENT_ID; usedDotEnv = true; }
    if (!clientSecret && dot.AZURE_CLIENT_SECRET) { clientSecret = dot.AZURE_CLIENT_SECRET; usedDotEnv = true; }
  }

  let usedFile = false;
  if ((!tenantId || !clientId || !clientSecret) && credsFile) {
    try {
      const p = isAbsolute(credsFile) ? credsFile : join(root, credsFile);
      const f = JSON.parse(readFileSync(p, "utf8")) as { tenantId?: string; clientId?: string; clientSecret?: string };
      if (!tenantId && f.tenantId) { tenantId = f.tenantId; usedFile = true; }
      if (!clientId && f.clientId) { clientId = f.clientId; usedFile = true; }
      if (!clientSecret && f.clientSecret) { clientSecret = f.clientSecret; usedFile = true; }
    } catch {
      /* missing/unreadable creds file — fall through to the missing-names report */
    }
  }

  if (!tenantId || !clientId || !clientSecret) {
    const missing: string[] = [];
    if (!tenantId) missing.push("AZURE_TENANT_ID");
    if (!clientId) missing.push("AZURE_CLIENT_ID");
    if (!clientSecret) missing.push("AZURE_CLIENT_SECRET");
    return {
      ok: false,
      missing,
      hint: `set ${missing.join(" / ")} (an exported env var, a gitignored root .env${credsFile ? `, or ${credsFile}` : ""}). The Azure AD app must be added in Partner Center → User management → Azure AD applications with the Manager role.`,
    };
  }

  // Cosmetic source tag (never the values): all-real-env or .env-only reads as "env"; anything the
  // JSON creds file contributed reads as "mixed".
  const source: StoreCreds["source"] = fromEnv ? "env" : usedFile ? "mixed" : usedDotEnv ? "env" : "mixed";
  return { ok: true, creds: { tenantId, clientId, clientSecret, source } };
}
