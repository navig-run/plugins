/**
 * Parse `wrangler whoami` output into the signed-in account, for the banner cache. Pure and
 * defensive — wrangler's format varies by version (OAuth vs API-token, with or without the account
 * table), so we extract an email if present and the first account-table row (name + 32-hex id) if
 * present, and return {} when neither is found (i.e. not logged in).
 */

export interface WhoamiInfo {
  email?: string;
  account?: string;
}

export function parseWhoami(stdout: string): WhoamiInfo {
  const info: WhoamiInfo = {};
  const email = stdout.match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/);
  if (email) info.email = email[0];
  for (const line of stdout.split(/\r?\n/)) {
    // A box-drawing table row: │ Account Name │ 0123abcd… (32-hex account id) │
    const m = line.match(/^\s*[│|]\s*([^│|]+?)\s*[│|]\s*([0-9a-f]{20,})\s*[│|]/i);
    if (m && !/account\s*name/i.test(m[1]!)) {
      info.account = m[1]!.trim();
      break;
    }
  }
  return info;
}
