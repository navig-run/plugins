/**
 * Derive the owning project for a command — the axis of the per-project menu view (`layout:
 * "projects"`). Generic: matches the command's id/label tokens against the *detected* workspace
 * package basenames (e.g. `apps/deck` → "deck", `web/www` → "www"), so `dev:deck` / "Deck · build"
 * both resolve to "deck". Repo-wide commands with no package token fall back to "workspace". No
 * hardcoded project names — everything comes from the workspace the scanner already found.
 */

function basenameToken(pkgPath: string): string {
  return pkgPath.split(/[\\/]/).filter(Boolean).pop()?.toLowerCase() ?? "";
}

export function projectFor(id: string, label: string, packages: string[]): string {
  const known = new Set(packages.map(basenameToken).filter(Boolean));
  if (known.size === 0) return "workspace";
  const tokens = [
    ...id.toLowerCase().split(/[:_\-./\s]+/),
    ...label.toLowerCase().split(/[^a-z0-9]+/),
  ].filter(Boolean);
  for (const t of tokens) if (known.has(t)) return t;
  return "workspace";
}
