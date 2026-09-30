/**
 * Microsoft Store listing & asset limits (single source of truth for this plugin).
 *
 * Screenshot size carries TWO thresholds on purpose: the submission engine accepts up to 50 MB
 * (what the Store's upload actually allows), while anything over 10 MB gets an advisory warning
 * (the sensible local quality gate). The legacy pipeline's two scripts disagreed on this; the
 * engine keeps both semantics explicit.
 */

export const LISTING_LIMITS = {
  description: { min: 200, max: 10000 },
  shortDescription: { max: 1000 },
  keywords: { min: 1, max: 7 }, // the Store keeps the first 7
  features: { max: 20 }, // the Store keeps the first 20
} as const;

export const SCREENSHOT_LIMITS = {
  count: { min: 1, max: 10 },
  px: { minW: 1366, maxW: 3840, minH: 768, maxH: 2160, recW: 1920, recH: 1080 },
  /** Hard upload limit — files over this are skipped. */
  hardMaxBytes: 50 * 1024 * 1024,
  /** Advisory limit — files over this upload fine but get a warning. */
  advisoryMaxBytes: 10 * 1024 * 1024,
  formats: ["png", "jpg", "jpeg"],
} as const;
