import { createHash } from "node:crypto";
import type { ScanResult } from "../detectors/fs.js";
import { isInterestingFile } from "../detectors/fs.js";
import { SCHEMA_VERSION } from "../config/constants.js";

/**
 * Stable fingerprint of the project's "interesting" files. If this is unchanged we can
 * trust the cached manifest and skip re-detection; any add/remove/edit of a manifest,
 * lockfile, workspace or framework config flips it. Cheap (mtime+size, not content hash).
 */
export function fingerprint(scan: ScanResult): string {
  const parts = scan.files
    .filter((f) => isInterestingFile(f.rel))
    .map((f) => `${f.rel}:${f.mtimeMs}:${f.size}`)
    .sort();
  const h = createHash("sha256");
  h.update(`v${SCHEMA_VERSION}\n`);
  h.update(parts.join("\n"));
  return "sha256:" + h.digest("hex").slice(0, 32);
}
