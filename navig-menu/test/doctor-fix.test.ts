import { describe, it, expect } from "vitest";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { editPackageScripts, brokenScriptPaths } from "../src/commands/index.js";
import { scanProject } from "../src/builder/build.js";
import { generateMenuDefinition } from "../src/builder/generate.js";

function scratch(files: Record<string, string>): string {
  const root = mkdtempSync(join(tmpdir(), "navig-menu-doctor-"));
  for (const [rel, content] of Object.entries(files)) {
    const abs = join(root, rel);
    mkdirSync(join(abs, ".."), { recursive: true });
    writeFileSync(abs, content, "utf8");
  }
  return root;
}

describe("editPackageScripts", () => {
  it("adds scripts, writes a timestamped backup, and preserves other keys", () => {
    const root = scratch({ "package.json": JSON.stringify({ name: "x", version: "1.0.0", scripts: { dev: "vite" } }, null, 2) + "\n" });
    try {
      const res = editPackageScripts(root, { typecheck: "tsc --noEmit" });
      expect(res.changed).toEqual(["typecheck"]);
      expect(res.backupPath).toBeDefined();

      const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
      expect(pkg.scripts).toEqual({ dev: "vite", typecheck: "tsc --noEmit" });
      expect(pkg.name).toBe("x"); // untouched keys preserved
      expect(pkg.version).toBe("1.0.0");

      const backups = readdirSync(join(root, ".navig", "backups"));
      expect(backups).toHaveLength(1);
      const original = JSON.parse(readFileSync(join(root, ".navig", "backups", backups[0]!), "utf8"));
      expect(original.scripts).toEqual({ dev: "vite" }); // backup holds the pre-edit content
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("is idempotent — an already-present value is a no-op with no backup", () => {
    const root = scratch({ "package.json": JSON.stringify({ scripts: { dev: "vite" } }, null, 2) + "\n" });
    try {
      const res = editPackageScripts(root, { dev: "vite" });
      expect(res.changed).toEqual([]);
      expect(res.backupPath).toBeUndefined();
      expect(readdirSync(root).includes(".navig")).toBe(false);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

describe("brokenScriptPaths", () => {
  it("flags a missing `cd <dir>` dir, ignores existing dirs, non-cd, and globs/vars", () => {
    const root = scratch({ "package.json": "{}", "apps/real/.keep": "" });
    try {
      const broken = brokenScriptPaths(root, {
        good: "cd apps/real && vite build",
        bad: "cd apps/gone && vite build",
        plain: "vite",
        glob: "cd apps/* && vite",
      });
      expect(broken.map((b) => b.id)).toEqual(["bad"]);
      expect(broken[0]!.dir).toBe("apps/gone");
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

describe("gap audit (generateMenuDefinition)", () => {
  it("reports missing canonicals and orphaned overrides", () => {
    const root = scratch({ "package.json": JSON.stringify({ scripts: { dev: "vite" } }, null, 2) + "\n" });
    try {
      const manifest = scanProject(root);
      const { audit } = generateMenuDefinition(manifest, {
        overrides: { dev: { label: "Dev" }, "ghost:script": { label: "Orphan" } },
      });
      expect(audit.missing).toContain("build"); // no build script exists
      expect(audit.missing).toContain("test");
      expect(audit.orphans).toEqual(["ghost:script"]); // targets a non-existent id
      expect(audit.orphans).not.toContain("dev"); // dev is a real detected script
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});
