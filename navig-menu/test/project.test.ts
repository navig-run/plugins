import { describe, it, expect } from "vitest";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { projectFor } from "../src/builder/project.js";
import { scanProject, loadDefinition, buildMenuModel } from "../src/builder/build.js";
import { resolveSettings } from "../src/ui/settings.js";
import { renderMenuSnapshot } from "../src/ui/menu.js";
import { createTheme } from "../src/ui/theme.js";

describe("projectFor", () => {
  const pkgs = ["apps/deck", "apps/os", "web/www", "packages/shared"];

  it("matches an id token to a workspace package basename", () => {
    expect(projectFor("dev:deck", "Deck · dev", pkgs)).toBe("deck");
    expect(projectFor("build:os", "OS build", pkgs)).toBe("os");
  });

  it("matches via the label when the id doesn't carry the package", () => {
    expect(projectFor("x:1", "Landing (www) dev", pkgs)).toBe("www");
  });

  it("falls back to 'workspace' for repo-wide commands or no packages", () => {
    expect(projectFor("sync:all", "Sync all", pkgs)).toBe("workspace");
    expect(projectFor("anything", "anything", [])).toBe("workspace");
  });
});

describe("project layout render", () => {
  it("groups commands by owning project when layout=projects", () => {
    const root = mkdtempSync(join(tmpdir(), "navig-menu-proj-"));
    try {
      writeFileSync(join(root, "pnpm-workspace.yaml"), "packages:\n  - apps/*\n", "utf8");
      writeFileSync(
        join(root, "package.json"),
        JSON.stringify({ name: "demo", scripts: { "dev:deck": "vite", "build:os": "vite build" } }, null, 2) + "\n",
        "utf8",
      );
      for (const p of ["apps/deck", "apps/os"]) {
        mkdirSync(join(root, p), { recursive: true });
        writeFileSync(join(root, p, "package.json"), JSON.stringify({ name: p.split("/")[1] }), "utf8");
      }
      const manifest = scanProject(root);
      const model = buildMenuModel(manifest, loadDefinition(root));
      const settings = { ...resolveSettings(model.ui, model.accent), layout: "projects" as const };
      const out = renderMenuSnapshot(
        model,
        { root, theme: createTheme({ plain: true }), mode: "local", git: { isRepo: false, dirty: 0 }, manifest, settings, toolVersion: "9.9.9" },
        { width: 80, height: 40 },
        { selected: 0, offset: 0, filter: "", view: "menu", settingsIndex: 0, customizerIndex: 0, customizerOffset: 0, pluginIndex: 0, pluginOffset: 0, category: null, pickerIndex: 0 } as never,
        undefined,
        settings,
      );
      expect(out).toContain("DECK");
      expect(out).toContain("OS");
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});
