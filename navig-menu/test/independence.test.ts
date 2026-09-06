import { describe, it, expect } from "vitest";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { scanProject, loadDefinition, buildMenuModel } from "../src/builder/build.js";
import { generateMenuDefinition } from "../src/builder/generate.js";

/**
 * The engine must work standalone on ANY project — no navig, no catalog, no `.navig/menu.json`.
 * This locks that contract so a NAVIG-specific assumption can't creep in.
 */
describe("independence — works on a plain non-navig project", () => {
  it("builds a usable menu with no catalog and no definition file", () => {
    const root = mkdtempSync(join(tmpdir(), "navig-menu-indep-"));
    try {
      writeFileSync(join(root, "pnpm-workspace.yaml"), "packages:\n  - packages/*\n", "utf8");
      writeFileSync(
        join(root, "package.json"),
        JSON.stringify(
          { name: "some-oss-lib", packageManager: "pnpm@9.0.0", scripts: { dev: "vite", build: "vite build", test: "vitest" } },
          null,
          2,
        ) + "\n",
        "utf8",
      );
      mkdirSync(join(root, "packages"), { recursive: true });

      const manifest = scanProject(root);
      expect(manifest.packageManager.value).toBe("pnpm"); // detected, not assumed
      expect(manifest.actions.length).toBeGreaterThan(0);

      // No .navig/menu.json present → loadDefinition returns empty, build still works.
      const load = loadDefinition(root);
      expect(load.def).toBeUndefined();
      const model = buildMenuModel(manifest, load);
      expect(model.title).toBe("some-oss-lib");
      expect(model.groups.length).toBeGreaterThan(0);
      expect(model.allScripts.some((a) => a.id === "dev")).toBe(true);
      // Detected scripts launch via the detected pm.
      expect(model.allScripts.find((a) => a.id === "dev")?.launcher).toBe("pnpm");

      // The gap audit runs without any navig catalog.
      const { audit } = generateMenuDefinition(manifest);
      expect(audit.mapped).toEqual(expect.arrayContaining(["dev -> dev", "build -> build", "test -> test"]));
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});
