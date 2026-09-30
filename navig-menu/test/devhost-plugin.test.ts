import { describe, it, expect } from "vitest";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { devhost, guessDomain, projectPorts, matchDomain } from "../src/plugins/builtin/devhost.js";
import { scanProject, buildMenuModel, loadDefinition } from "../src/builder/build.js";
import type { PluginContext } from "../src/plugins/types.js";

/** Build the full menu model for a project root — the real BUILTIN_PLUGINS path. */
function modelFor(root: string) {
  return buildMenuModel(scanProject(root), loadDefinition(root));
}

/** A minimal PluginContext for the pure detect/contribute paths. */
function ctx(opts: { root?: string; endpoints?: { url: string }[]; pkg?: boolean } = {}): PluginContext {
  const { root = "/tmp/my-app", endpoints = [], pkg = false } = opts;
  return {
    root,
    manifest: { endpoints },
    scripts: {},
    hasFile: (rel: string) => (pkg ? rel === "package.json" : false),
    hasDep: () => false,
    settings: {},
    cacheDir: "/tmp/cache",
    readCache: () => undefined,
  } as unknown as PluginContext;
}

describe("devhost plugin · pure helpers", () => {
  it("guessDomain slugifies the project folder → <slug>.test", () => {
    expect(guessDomain({ root: "/x/My Cool App" })).toBe("my-cool-app.test");
    expect(guessDomain({ root: "/x/web_pocket" })).toBe("web-pocket.test");
    expect(guessDomain({ root: "" })).toBe("app.test");
  });

  it("projectPorts parses ports from endpoint urls, deduped", () => {
    expect(
      projectPorts({ manifest: { endpoints: [{ url: "http://localhost:3000/" }, { url: "http://127.0.0.1:3000" }, { url: "http://localhost:8080" }] } }),
    ).toEqual([3000, 8080]);
    expect(projectPorts({ manifest: { endpoints: [] } })).toEqual([]);
    expect(projectPorts({})).toEqual([]);
  });

  it("matchDomain: a registry entry on a project port wins; otherwise a guess (configured:false)", () => {
    const reg = { domains: { "api.test": { target_port: 3000 } } };
    expect(matchDomain({ root: "/x/app", manifest: { endpoints: [{ url: "http://localhost:3000" }] } }, reg)).toEqual({
      domain: "api.test",
      target_port: 3000,
      configured: true,
    });
    expect(matchDomain({ root: "/x/My App", manifest: { endpoints: [{ url: "http://localhost:9999" }] } }, reg)).toEqual({
      domain: "my-app.test",
      target_port: 9999,
      configured: false,
    });
    expect(matchDomain({ root: "/x/solo", manifest: { endpoints: [] } }, null)).toEqual({
      domain: "solo.test",
      target_port: undefined,
      configured: false,
    });
  });
});

describe("devhost plugin · surface", () => {
  it("has the expected identity and tier", () => {
    expect(devhost.id).toBe("devhost");
    expect(devhost.tier).toBe("programmatic");
  });

  it("detects web projects (a served port OR a package.json), else stays off", () => {
    expect(devhost.detect?.(ctx({ endpoints: [{ url: "http://localhost:3000" }] }))).toBe(true);
    expect(devhost.detect?.(ctx({ pkg: true }))).toBe(true);
    expect(devhost.detect?.(ctx({ endpoints: [], pkg: false }))).toBe(false);
  });

  it("contribute yields the Dev Host section with the five actions", () => {
    const c = devhost.contribute(ctx({ root: "/x/my-app", endpoints: [{ url: "http://localhost:3000" }] }));
    const section = c.sections?.find((s) => s.group === "Dev Host");
    expect(section).toBeDefined();
    expect(section!.actions.map((a) => a.internal)).toEqual(["setup", "up", "open", "status", "remove"]);
  });
});

describe("devhost plugin · menu-model integration (proves it is wired into BUILTIN_PLUGINS)", () => {
  it("auto-activates for a web project and contributes the Dev Host section", () => {
    const root = mkdtempSync(join(tmpdir(), "navig-devhost-"));
    try {
      writeFileSync(join(root, "package.json"), JSON.stringify({ name: "app", scripts: { dev: "vite" } }), "utf8");
      const model = modelFor(root);
      const dh = model.loadedPlugins?.find((p) => p.id === "devhost");
      expect(dh?.active).toBe(true);
      expect(dh?.auto).toBe(true); // activated by detect(), not an explicit enable
      expect(model.groups.some((g) => g.group === "Dev Host")).toBe(true);
      // the five handler actions are appended as internal plugin actions
      expect(model.allScripts.some((a) => a.source.startsWith("plugin:internal:devhost:"))).toBe(true);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("stays inactive for a non-web project (no package.json, no served port)", () => {
    const root = mkdtempSync(join(tmpdir(), "navig-nodevhost-"));
    try {
      const model = modelFor(root);
      expect(model.loadedPlugins?.find((p) => p.id === "devhost")?.active).toBe(false);
      expect(model.groups.some((g) => g.group === "Dev Host")).toBe(false);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});
