/**
 * Pure dotenv helpers. SECURITY: `parseEnvKeys` returns only variable NAMES — it splits each line at
 * the first `=` and keeps the left side, discarding the value entirely. No value is ever returned,
 * stored, or logged (the env-doctor ground rule: names only, never values). `buildEnvAppend` produces
 * empty `KEY=` lines for scaffolding, so it never writes a value either.
 */

/** Extract declared variable NAMES from a .env body (values discarded). Handles `export`, comments. */
export function parseEnvKeys(text: string): string[] {
  const seen = new Set<string>();
  const keys: string[] = [];
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const m = line.replace(/^export\s+/, "").match(/^([A-Za-z_][A-Za-z0-9_]*)\s*=/);
    if (m && !seen.has(m[1]!)) {
      seen.add(m[1]!);
      keys.push(m[1]!); // ONLY the name — the value after `=` is never captured
    }
  }
  return keys;
}

/** Append empty `KEY=` lines for the missing names to the existing target content (no values). */
export function buildEnvAppend(existing: string, missing: string[]): string {
  if (!missing.length) return existing;
  let content = existing;
  if (content && !content.endsWith("\n")) content += "\n";
  return content + missing.map((k) => `${k}=`).join("\n") + "\n";
}
