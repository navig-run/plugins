/**
 * Screenshot validation without native image deps: a pure PNG/JPEG header probe plus the
 * skip/warn decision logic. The engine does NOT resize (the legacy pipeline used System.Drawing);
 * oversized files are flagged so the caller can offer an external downscale (e.g. ImageMagick)
 * or skip with an actionable message.
 */

import { SCREENSHOT_LIMITS } from "./limits.js";

export interface ImageSize {
  width: number;
  height: number;
  format: "png" | "jpeg";
}

/** Read pixel dimensions from PNG IHDR or JPEG SOF headers. Null when not a readable PNG/JPEG. */
export function probeImageSize(buf: Uint8Array): ImageSize | null {
  if (buf.length >= 24 && buf[0] === 0x89 && buf[1] === 0x50 && buf[2] === 0x4e && buf[3] === 0x47) {
    // PNG: 8-byte signature, then the IHDR chunk — width/height are big-endian u32 at 16/20.
    return { width: readU32BE(buf, 16), height: readU32BE(buf, 20), format: "png" };
  }
  if (buf.length >= 4 && buf[0] === 0xff && buf[1] === 0xd8) {
    // JPEG: walk marker segments until a Start-Of-Frame (C0–CF except C4/C8/CC).
    let off = 2;
    while (off + 9 < buf.length) {
      if (buf[off] !== 0xff) {
        off++;
        continue;
      }
      const marker = buf[off + 1]!;
      if (marker === 0xff) {
        off++;
        continue;
      }
      if (marker >= 0xc0 && marker <= 0xcf && marker !== 0xc4 && marker !== 0xc8 && marker !== 0xcc) {
        return { height: readU16BE(buf, off + 5), width: readU16BE(buf, off + 7), format: "jpeg" };
      }
      const len = readU16BE(buf, off + 2);
      if (len < 2) return null;
      off += 2 + len;
    }
  }
  return null;
}

export interface ScreenshotVerdict {
  /** "ok" → upload as-is; "oversize" → dimensions exceed max (downscale or skip); "skip" → unusable. */
  verdict: "ok" | "oversize" | "skip";
  reason?: string;
  warnings: string[];
}

/** Apply the Store's screenshot rules to one candidate file. */
export function evaluateScreenshot(info: { name: string; bytes: number; width: number; height: number }): ScreenshotVerdict {
  const { px, hardMaxBytes, advisoryMaxBytes } = SCREENSHOT_LIMITS;
  const warnings: string[] = [];
  if (info.width < px.minW || info.height < px.minH) {
    return {
      verdict: "skip",
      reason: `${info.name} is ${info.width}x${info.height} — below the Store minimum ${px.minW}x${px.minH}`,
      warnings,
    };
  }
  if (info.bytes > hardMaxBytes) {
    return {
      verdict: "skip",
      reason: `${info.name} is ${(info.bytes / 1024 / 1024).toFixed(1)} MB — over the ${hardMaxBytes / 1024 / 1024} MB upload limit`,
      warnings,
    };
  }
  if (info.bytes > advisoryMaxBytes) {
    warnings.push(`${info.name} is ${(info.bytes / 1024 / 1024).toFixed(1)} MB — consider keeping screenshots under ${advisoryMaxBytes / 1024 / 1024} MB`);
  }
  if (info.width > px.maxW || info.height > px.maxH) {
    return {
      verdict: "oversize",
      reason: `${info.name} is ${info.width}x${info.height} — over the Store maximum ${px.maxW}x${px.maxH}; downscale it (e.g. with ImageMagick) or it will be skipped`,
      warnings,
    };
  }
  return { verdict: "ok", warnings };
}

function readU32BE(buf: Uint8Array, off: number): number {
  return ((buf[off]! << 24) | (buf[off + 1]! << 16) | (buf[off + 2]! << 8) | buf[off + 3]!) >>> 0;
}

function readU16BE(buf: Uint8Array, off: number): number {
  return (buf[off]! << 8) | buf[off + 1]!;
}
