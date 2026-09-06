import { describe, it, expect } from "vitest";
import { createTheme } from "../src/ui/theme.js";
import { header, status, kv, hint, bullet } from "../src/ui/report.js";

// Plain theme → deterministic ASCII, no color codes, so we can assert on exact text.
const theme = createTheme({ plain: true });

describe("report formatter (plain)", () => {
  it("header renders the title over a rule at least as wide as the title", () => {
    const out = header(theme, "doctor");
    const [title, rule] = out.split("\n");
    expect(title).toBe("  doctor");
    expect(rule.trim()).toMatch(/^-{10,}$/); // ASCII rule, min width 10
  });

  it("status uses the ASCII glyph for each kind (never colour-only)", () => {
    expect(status(theme, "ok", "done")).toBe("  v done");
    expect(status(theme, "warn", "careful")).toBe("  ! careful");
    expect(status(theme, "fail", "broke")).toBe("  x broke");
    expect(status(theme, "muted", "absent")).toBe("  o absent");
    expect(status(theme, "add", "added")).toBe("  + added");
    expect(status(theme, "remove", "pruned")).toBe("  - pruned");
  });

  it("kv pads the label column and supports nesting depth", () => {
    expect(kv(theme, "imported", "63 commands")).toBe("  imported     63 commands");
    expect(kv(theme, "shell-op", "x", { depth: 1 })).toBe("    shell-op     x");
  });

  it("hint and bullet indent and mark correctly", () => {
    expect(hint(theme, "note")).toBe("  note");
    expect(bullet(theme, "item")).toBe("  - item");
    expect(bullet(theme, "nested", 1)).toBe("    - nested");
  });
});
