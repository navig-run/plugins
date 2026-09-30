/**
 * Azure blob SAS URL freshness. A draft submission left by an earlier run carries a time-limited
 * `fileUploadUrl`; reusing an expired one fails the package upload with
 * "AuthenticationFailed ... expiry time must be after start time".
 */

const MARGIN_MS = 5 * 60 * 1000;

/** True if the SAS upload URL has more than 5 minutes of validity left. Unparseable → stale. */
export function isSasUrlFresh(url: string | undefined | null, nowMs: number = Date.now()): boolean {
  if (!url || url.trim() === "") return false;
  let query: string;
  try {
    query = new URL(url).search.replace(/^\?/, "");
  } catch {
    return false; // not a URL → treat as stale (recreate to be safe)
  }
  if (!query) return true;
  const sePair = query.split("&").find((p) => p.startsWith("se="));
  if (!sePair) return true; // no expiry param → assume usable
  const raw = decodeURIComponent(sePair.slice(3));
  // SAS `se=` is ISO-8601; a value without an explicit offset means UTC.
  const iso = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(raw) ? raw : raw + "Z";
  const expiry = Date.parse(iso);
  if (Number.isNaN(expiry)) return false;
  return expiry > nowMs + MARGIN_MS;
}
