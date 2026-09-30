/**
 * Submission upload zip: a flat-root archive containing every file referenced by `fileName`
 * in the submission body (the package plus any screenshots marked PendingUpload) — the same
 * layout the Store's blob ingestion expects. Packages are already compressed, so they're
 * STOREd (level 0); images get normal deflate.
 */

import { zipSync } from "fflate";
import type { UploadFile } from "./types.js";

const STORED_EXT = /\.(msixupload|msixbundle|appxupload|msix|appx)$/i;

export function buildSubmissionZip(files: UploadFile[]): Uint8Array {
  const entries: Record<string, [Uint8Array, { level: 0 | 6 }]> = {};
  for (const f of files) {
    const leaf = f.name.split(/[\\/]/).pop()!;
    entries[leaf] = [f.data, { level: STORED_EXT.test(leaf) ? 0 : 6 }];
  }
  return zipSync(entries);
}
