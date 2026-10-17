/**
 * Upload the submission zip to the draft's Azure blob SAS URL. The SAS URL already carries
 * auth — send only the blob-type header, no bearer token. Errors never echo the URL (its
 * query string contains the SAS signature).
 */

import type { EngineIO } from "./types.js";

export async function uploadToSas(io: EngineIO, sasUrl: string, zip: Uint8Array): Promise<void> {
  let res: Response;
  try {
    res = await io.fetch(sasUrl, {
      method: "PUT",
      headers: { "x-ms-blob-type": "BlockBlob", "Content-Type": "application/zip" },
      body: zip as unknown as NonNullable<Parameters<typeof globalThis.fetch>[1]>["body"],
    });
  } catch (e) {
    throw new Error(`package upload failed: network error (${(e as Error).message})`);
  }
  if (!res.ok) {
    let detail = "";
    try {
      detail = (await res.text()).slice(0, 500);
    } catch {
      /* unreadable body */
    }
    throw new Error(`package upload failed: HTTP ${res.status}${detail ? `\n${detail}` : ""}`);
  }
}
