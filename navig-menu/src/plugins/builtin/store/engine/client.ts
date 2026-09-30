/**
 * Token acquisition + retrying REST client for the DevCenter/Partner Center submission API
 * (manage.devcenter.microsoft.com/v1.0/my).
 *
 * Retry semantics (ported from the battle-tested pipeline): HTTP 429 and the gateway/timeout
 * family (408/500/502/503/504) retry with Retry-After (header, then the "Try again in N second"
 * body phrase) or exponential backoff 8→128 s capped at 300, plus a 3 s buffer capped at 330.
 * A 5xx on a non-idempotent POST MIGHT have landed server-side, so callers that can't tolerate
 * a double-submit (the add-on commit) pass `maxRetries: 0` and re-check server state themselves.
 */

import type { EngineIO, StoreCreds } from "./types.js";

export const STORE_API_BASE = "https://manage.devcenter.microsoft.com/v1.0/my";
const TRANSIENT = new Set([408, 429, 500, 502, 503, 504]);

/** Azure AD client-credentials token. Errors are sanitized — the secret never appears in them. */
export async function acquireToken(creds: StoreCreds, io: EngineIO): Promise<string> {
  const body = new URLSearchParams({
    grant_type: "client_credentials",
    client_id: creds.clientId,
    client_secret: creds.clientSecret,
    resource: "https://manage.devcenter.microsoft.com",
  });
  let res: Response;
  try {
    res = await io.fetch(`https://login.microsoftonline.com/${creds.tenantId}/oauth2/token`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body.toString(),
    });
  } catch (e) {
    const msg = (e as Error).message + " " + String((e as { cause?: { code?: string } }).cause?.code ?? "");
    // WSAEACCES on login.microsoftonline.com is NOT a credentials problem — a local firewall
    // (e.g. WindowsSpyBlocker's "Blocker Microsoft*" rules) is blocking the Azure AD login pool.
    if (/EACCES|EPERM|forbidden by its access permissions|10013/i.test(msg)) {
      throw new Error(
        "Azure AD login blocked by a local firewall (socket access forbidden on login.microsoftonline.com). This is NOT a credentials issue — a telemetry/privacy blocker is likely blocking the Azure AD login IPs (20.190.x / 40.126.x). Temporarily disable those firewall rules and retry.",
      );
    }
    throw new Error(`Azure AD token request failed: network error (${(e as Error).message}). Check connectivity to login.microsoftonline.com.`);
  }
  if (!res.ok) {
    // Sanitized: surface only the AAD error code/description — never echo the request body.
    let detail = "";
    try {
      const j = (await res.json()) as { error?: string; error_description?: string };
      detail = [j.error, j.error_description?.split("\r\n")[0]].filter(Boolean).join(": ");
    } catch {
      /* non-JSON error body — omit */
    }
    throw new Error(
      `Azure AD token request failed: HTTP ${res.status}${detail ? ` (${detail})` : ""}. Verify the tenant/client ids and that the app is added in Partner Center → User management → Azure AD applications.`,
    );
  }
  const json = (await res.json()) as { access_token?: string };
  if (!json.access_token) throw new Error("No access token returned from Azure AD.");
  return json.access_token;
}

/** Pure: how many seconds to wait before retry `attempt` (1-based) of a transient failure. */
export function computeRetryDelay(code: number, attempt: number, retryAfterHeader?: string | null, bodyText?: string): number {
  let delay = 0;
  if (code === 429) {
    const h = Number(retryAfterHeader);
    if (Number.isFinite(h) && h > 0) delay = Math.ceil(h);
    if (delay <= 0 && bodyText) {
      const m = /Try again in (\d+)\s*second/.exec(bodyText);
      if (m) delay = Number(m[1]);
    }
  }
  if (delay <= 0) delay = Math.min(300, Math.pow(2, attempt + 2)); // 8, 16, 32, 64, 128 …
  return Math.min(delay + 3, 330); // small buffer past the server's countdown; hard cap 5.5 min
}

/** Pure: actionable hint suffix for a non-transient (or retry-exhausted) HTTP status. */
export function classifyHttpError(code: number, maxRetries?: number): string {
  switch (code) {
    case 403:
      return " (the seller account / Azure AD app can't access this resource, or the id is wrong)";
    case 404:
      return " (resource not found — check the id)";
    case 409:
      return " (a submission is already in progress for this resource)";
    case 429:
      return ` (rate limit exceeded; exhausted ${maxRetries ?? "all"} retries — wait a few minutes and re-run)`;
    default:
      return "";
  }
}

export class StoreApiError extends Error {
  constructor(
    message: string,
    public readonly status: number | undefined,
    public readonly detail: string,
  ) {
    super(message);
    this.name = "StoreApiError";
  }
}

export interface StoreClient {
  request<T = unknown>(method: string, path: string, body?: unknown, opts?: { maxRetries?: number }): Promise<T>;
  /** Pre-warm the adaptive throttle to a minimum inter-request gap (ms). Call before a known
   *  rate-limited batch (e.g. the analytics sweep) so it paces from the first call rather than
   *  learning the limit via a 429. The sticky floor still adapts upward if the Store needs more. */
  pace(minMs: number): void;
}

export function createClient(token: string, io: EngineIO): StoreClient {
  // Adaptive throttle shared across every request from this client: the first 429 teaches the
  // batch the Store's current rate limit, so sibling calls (e.g. the 9-app analytics sweep) pace
  // themselves instead of each eating a 429 → retry round-trip. It decays back to full speed as
  // calls succeed, and is a complete no-op until the first 429 (paceMs stays 0).
  let paceMs = 0;
  let floorMs = 0; // sticky minimum gap once a 429 is seen (server's raw Retry-After) — the pace
  //                  never decays below this in-batch, so it stops dipping under the limit.
  let nextAt = 0;
  return {
    pace(minMs: number): void {
      floorMs = Math.min(12_000, Math.max(floorMs, minMs));
      paceMs = Math.max(paceMs, floorMs);
    },
    async request<T>(method: string, path: string, body?: unknown, opts?: { maxRetries?: number }): Promise<T> {
      const maxRetries = opts?.maxRetries ?? 5;
      const uri = `${STORE_API_BASE}/${path}`;
      // Serialize once so retries don't re-convert. fetch sends strings as UTF-8, so the legacy
      // PS5.1/PS7 byte-array dance is unnecessary here.
      const json = body === undefined || body === null ? undefined : JSON.stringify(body);
      const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
      if (json !== undefined) headers["Content-Type"] = "application/json; charset=utf-8";

      let attempt = 0;
      for (;;) {
        // Proactive pace — honor the learned rate before firing (no-op while paceMs is 0).
        const wait = nextAt - io.now();
        if (wait > 0) await io.sleep(wait);

        let res: Response;
        try {
          res = await io.fetch(uri, { method, headers, body: json });
        } catch (e) {
          throw new StoreApiError(`Store API ${method} ${path} -> network error: ${(e as Error).message}`, undefined, "");
        }
        if (res.ok) {
          paceMs = Math.max(floorMs, paceMs - 250); // recover slowly, but never below the learned floor
          nextAt = io.now() + paceMs;               // schedule the next request
          const text = await res.text();
          if (!text) return undefined as T;
          try {
            return JSON.parse(text) as T;
          } catch {
            return text as unknown as T;
          }
        }

        const code = res.status;
        let detail = "";
        try {
          detail = await res.text();
        } catch {
          /* unreadable error body */
        }

        if (TRANSIENT.has(code) && attempt < maxRetries) {
          attempt++;
          const delay = computeRetryDelay(code, attempt, res.headers.get("retry-after"), detail);
          // Teach the batch the limit so the rest of the sweep paces itself. The server's raw
          // Retry-After becomes a sticky floor (paceMs never decays below it), which stops the
          // pace from dipping under the limit and re-triggering a 429 every few calls.
          if (code === 429) {
            const ra = Number(res.headers.get("retry-after"));
            const raMs = Number.isFinite(ra) && ra > 0 ? ra * 1_000 : delay * 1_000;
            floorMs = Math.min(12_000, Math.max(floorMs, raMs));
            paceMs = Math.max(paceMs, floorMs);
          }
          const label = code === 429 ? "Rate limited (HTTP 429)" : `Transient HTTP ${code} (gateway/timeout)`;
          const shortPath = path.split("?")[0]; // query strings (analytics filters) are noise here
          io.warn(`${label} on ${method} ${shortPath} — waiting ${delay}s, then retry ${attempt}/${maxRetries}...`);
          await io.sleep(delay * 1000);
          continue;
        }

        throw new StoreApiError(`Store API ${method} ${path} -> HTTP ${code}${classifyHttpError(code, maxRetries)}\n${detail}`.trim(), code, detail);
      }
    },
  };
}
