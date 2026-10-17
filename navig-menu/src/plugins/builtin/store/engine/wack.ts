/**
 * Windows App Certification Kit (WACK / appcert.exe) — the local pre-submission cert pass the
 * Store runs server-side, so catching failures here saves a rejected submission round-trip.
 *
 * Pure + testable: {@link pickWackTarget} chooses the right artifact and {@link parseWackReport}
 * reads appcert's XML verdict. Locating appcert.exe and running it (Windows + SDK only, needs
 * elevation) lives in the handler.
 */

/** appcert tests an installable package, NOT the .msixupload zip — prefer a bundle, then a package. */
export function pickWackTarget(fileNames: string[]): string | undefined {
  const lower = (n: string) => n.toLowerCase();
  const byExt = (exts: string[]) => fileNames.filter((n) => exts.some((e) => lower(n).endsWith(e)));
  // Bundles first (cover all architectures), then single packages. .msixupload/.appxupload are
  // zips of a bundle — appcert can't test them directly, so they're excluded.
  const bundles = byExt([".msixbundle", ".appxbundle"]);
  if (bundles.length) return bundles.sort().at(-1);
  const packages = byExt([".msix", ".appx"]);
  if (packages.length) return packages.sort().at(-1);
  return undefined;
}

export interface WackVerdict {
  overall: "PASS" | "FAIL" | "WARNING" | "UNKNOWN";
  failed: string[];
  warnings: string[];
}

/**
 * Parse an appcert report XML. The kit writes `<REPORT OVERALL_RESULT="PASS|FAIL|WARNING">` with
 * nested `<TEST NAME="…" RESULT="…">` entries. We surface the overall verdict plus the names of any
 * FAIL/WARNING tests — tolerant of attribute order and casing.
 */
export function parseWackReport(xml: string): WackVerdict {
  const overallRaw = /OVERALL_RESULT\s*=\s*"([^"]*)"/i.exec(xml)?.[1]?.toUpperCase();
  const overall: WackVerdict["overall"] =
    overallRaw === "PASS" || overallRaw === "FAIL" || overallRaw === "WARNING" ? overallRaw : "UNKNOWN";

  const failed: string[] = [];
  const warnings: string[] = [];
  // Match each TEST element's attributes (order-independent) and pull NAME + RESULT.
  const testRe = /<TEST\b([^>]*)>/gi;
  let m: RegExpExecArray | null;
  while ((m = testRe.exec(xml)) !== null) {
    const attrs = m[1] ?? "";
    const result = /RESULT\s*=\s*"([^"]*)"/i.exec(attrs)?.[1]?.toUpperCase();
    if (result !== "FAIL" && result !== "WARNING") continue;
    const name = /NAME\s*=\s*"([^"]*)"/i.exec(attrs)?.[1] ?? "(unnamed test)";
    (result === "FAIL" ? failed : warnings).push(name);
  }
  return { overall, failed, warnings };
}
