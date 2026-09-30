import { describe, it, expect } from "vitest";
import {
  buildListingUser,
  buildListingPrompt,
  parseListing,
  normalizeListing,
  type ListingGenContext,
} from "../src/plugins/builtin/store/engine/listinggen.js";

const ctx: ListingGenContext = {
  name: "Blindspot Guard",
  identity: "CybesisStudios.BlindspotGuard",
  category: "Security",
  stack: ["Windows", "Tauri"],
  context: "A scanner for browser extensions. Finds risky permissions and leaked keys.",
  year: 2026,
  studio: "Cybesis Studios",
};

describe("buildListingUser / buildListingPrompt", () => {
  it("carries name, category, and the context verbatim", () => {
    const u = buildListingUser(ctx);
    expect(u).toContain("App name: Blindspot Guard");
    expect(u).toContain("Store category: Security");
    expect(u).toContain("browser extensions");
  });
  it("prompt embeds the JSON-only system instruction", () => {
    expect(buildListingPrompt(ctx)).toMatch(/ONLY minified JSON/);
  });
});

describe("parseListing", () => {
  it("extracts a JSON object even with surrounding prose/fences", () => {
    const got = parseListing('Sure!\n```json\n{"description":"x","features":["a"]}\n```');
    expect(got).toEqual({ description: "x", features: ["a"] });
  });
  it("returns null when there is no JSON object", () => {
    expect(parseListing("no json here")).toBeNull();
  });
});

describe("normalizeListing", () => {
  it("sets displayName + category from trusted opts, never the model", () => {
    const meta = normalizeListing({ displayName: "HACKED", applicationCategory: "Games" }, {
      displayName: "Blindspot Guard",
      category: "Security",
      year: 2026,
      studio: "Cybesis Studios",
    });
    expect(meta.displayName).toBe("Blindspot Guard");
    expect(meta.applicationCategory).toBe("Security");
  });

  it("hard-clamps features (<=20) and keywords (<=7)", () => {
    const raw = {
      features: Array.from({ length: 40 }, (_, i) => `feature ${i}`),
      keywords: Array.from({ length: 20 }, (_, i) => `kw${i}`),
    };
    const meta = normalizeListing(raw, { displayName: "X", year: 2026 });
    expect(meta.features!.length).toBe(20);
    expect(meta.keywords!.length).toBe(7);
  });

  it("clamps shortDescription to 200 chars and drops empty fields", () => {
    const meta = normalizeListing({ shortDescription: "x".repeat(500), description: "  " }, { displayName: "X", year: 2026 });
    expect(meta.shortDescription!.length).toBe(200);
    expect(meta.description).toBeUndefined();
  });

  it("defaults copyright + devStudio from the studio when the model omits them", () => {
    const meta = normalizeListing({}, { displayName: "X", year: 2026, studio: "Cybesis Studios" });
    expect(meta.copyrightInfo).toBe("(c) 2026 Cybesis Studios");
    expect(meta.devStudio).toBe("Cybesis Studios");
  });

  it("maps notes.whatsNew", () => {
    const meta = normalizeListing({ notes: { whatsNew: "First release." } }, { displayName: "X", year: 2026 });
    expect(meta.notes?.whatsNew).toBe("First release.");
  });
});
