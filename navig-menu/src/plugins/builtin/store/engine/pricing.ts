/**
 * Store price-tier mapping. Microsoft runs TWO tier schemes and which one an account uses is
 * reported by the PUBLISHED APP submission's read-only `pricing.isAdvancedPricingModel` flag
 * (a freshly-created add-on submission misreports it). Tier ids are not shared between schemes —
 * a tier valid in one is rejected as "Price Tier is not supported" in the other.
 *
 *  - Advanced (isAdvancedPricingModel = true): valid tiers Tier1012–Tier1424; the low band
 *    ($0.99–$9.99) steps uniformly by $0.10 from the documented floor Tier1012 = $0.99,
 *    so tier# = 1012 + round((amount − 0.99) / 0.10) → $2.99 = Tier1032.
 *  - Legacy (isAdvancedPricingModel = false): valid tiers Tier2–Tier96, $X.99 anchors stepping
 *    one tier per dollar from Tier2 = $0.99.
 */

const ADVANCED_ANCHORS: Record<string, string> = {
  "0.99": "Tier1012", "1.99": "Tier1022", "2.99": "Tier1032", "3.99": "Tier1042", "4.99": "Tier1052",
  "5.99": "Tier1062", "6.99": "Tier1072", "7.99": "Tier1082", "8.99": "Tier1092", "9.99": "Tier1102",
};

const LEGACY_ANCHORS: Record<string, string> = {
  "0.99": "Tier2", "1.99": "Tier3", "2.99": "Tier4", "3.99": "Tier5", "4.99": "Tier6",
  "5.99": "Tier7", "6.99": "Tier8", "7.99": "Tier9", "8.99": "Tier10", "9.99": "Tier11",
};

export interface TierResult {
  tier: string;
  /** False when the amount fell outside the mapped band and the nearest anchor was used. */
  exact: boolean;
  note?: string;
}

/** Map a USD amount to a base price-tier id for the given scheme. */
export function usdToTier(amount: number, advanced: boolean): TierResult {
  if (amount <= 0) return { tier: "Free", exact: true };
  const key = amount.toFixed(2);

  if (advanced) {
    const anchor = ADVANCED_ANCHORS[key];
    if (anchor) return { tier: anchor, exact: true };
    if (amount >= 0.99 && amount <= 9.99) {
      const steps = Math.round((amount - 0.99) / 0.1);
      return { tier: `Tier${1012 + steps}`, exact: true };
    }
    const nearest = nearestAnchor(amount, ADVANCED_ANCHORS);
    return {
      tier: nearest.tier,
      exact: false,
      note: `no verified advanced price tier for $${key}; using nearest anchor $${nearest.amount} → ${nearest.tier}. Confirm in Partner Center → Pricing → view table before publishing.`,
    };
  }

  const anchor = LEGACY_ANCHORS[key];
  if (anchor) return { tier: anchor, exact: true };
  const nearest = nearestAnchor(amount, LEGACY_ANCHORS);
  return {
    tier: nearest.tier,
    exact: false,
    note: `no exact legacy price tier for $${key}; using nearest anchor $${nearest.amount} → ${nearest.tier}. Confirm in Partner Center.`,
  };
}

/** Inverse mapping: decode a tier id back to a USD amount. Null for Base/NotAvailable/unknown. */
export function tierToUsd(tier: string, advanced: boolean): number | null {
  if (!tier || tier.trim() === "" || tier === "Free") return 0;
  const m = /^Tier(\d+)$/.exec(tier);
  if (!m) return null;
  const n = Number(m[1]);
  const usd = advanced ? 0.99 + (n - 1012) * 0.1 : 0.99 + (n - 2) * 1.0;
  return Math.round(usd * 100) / 100;
}

function nearestAnchor(amount: number, anchors: Record<string, string>): { amount: string; tier: string } {
  let best: { amount: string; tier: string } | undefined;
  let bestDist = Number.POSITIVE_INFINITY;
  for (const [amt, tier] of Object.entries(anchors)) {
    const d = Math.abs(Number(amt) - amount);
    if (d < bestDist) {
      bestDist = d;
      best = { amount: amt, tier };
    }
  }
  return best!;
}
