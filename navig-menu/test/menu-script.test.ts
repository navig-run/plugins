import { describe, it, expect } from "vitest";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pmExecRunner, ensureMenuScript } from "../src/commands/index.js";
import { createTheme } from "../src/ui/theme.js";

const theme = createTheme({ plain: true });
const scratch = (pkg: object): string => {
  const root = mkdtempSync(join(tmpdir(), "navig-menu-pm-"));
  writeFileSync(join(root, "package.json"), JSON.stringify(pkg, null, 2) + "\n", "utf8");
  return root;
};
const readScripts = (root: string): Record<string, string> =>
  JSON.parse(readFileSync(join(root, "package.json"), "utf8")).scripts;

describe("pmExecRunner", () => {
  it("maps each package manager to its registry exec-runner", () => {
    expect(pmExecRunner("npm")).toBe("npx --yes navig-menu");
    expect(pmExecRunner("none")).toBe("npx --yes navig-menu");
    expect(pmExecRunner("pnpm")).toBe("pnpm dlx navig-menu");
    expect(pmExecRunner("yarn")).toBe("yarn dlx navig-menu");
    expect(pmExecRunner("bun")).toBe("bunx navig-menu");
  });
});

describe("ensureMenuScript — never overwrites a hand-written `menu`", () => {
  it("keeps a hand-written `menu` and only adds the `menu:navig` alias", async () => {
    const root = scratch({ scripts: { menu: "node scripts/menu.js", dev: "vite" } });
    try {
      const result = await ensureMenuScript(root, theme, { assumeYes: true, pm: "pnpm" });
      const s = readScripts(root);
      expect(result).toBe("alias-added");
      expect(s.menu).toBe("node scripts/menu.js"); // untouched
      expect(s["menu:navig"]).toBeDefined(); // alias added
      expect(s.dev).toBe("vite"); // unrelated script untouched
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("keeps any custom `menu` value untouched", async () => {
    const root = scratch({ scripts: { menu: "make menu" } });
    try {
      await ensureMenuScript(root, theme, { assumeYes: true, pm: "bun" });
      expect(readScripts(root).menu).toBe("make menu");
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("recognizes a pm-generated value as ours and refreshes it (not treated as hand-written)", async () => {
    // `npx navig-menu` is a known generated form but never the *current* command (which carries
    // `--yes` or is a `node <path>`), so a recognized value is provably refreshed, not preserved.
    const root = scratch({ scripts: { menu: "npx navig-menu" } });
    try {
      await ensureMenuScript(root, theme, { assumeYes: true, pm: "npm" });
      const s = readScripts(root);
      expect(s.menu).toBe(s["menu:navig"]); // Branch A set both to the current command
      expect(s.menu).not.toBe("npx navig-menu"); // refreshed, not kept as a hand-written value
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});
