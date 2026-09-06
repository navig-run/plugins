import { describe, it, expect } from "vitest";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { scanProject, buildMenuModel, loadDefinition } from "../src/builder/build.js";
import { loadPlugins } from "../src/plugins/loader.js";
import { parseNetstat, parseLsof, parseTasklist, parseRange, findFreePort, endpointPorts, upsertEnvPort } from "../src/plugins/builtin/ports.js";
import { savePluginState } from "../src/ui/settings.js";
import { createTheme } from "../src/ui/theme.js";
import { renderMenuSnapshot } from "../src/ui/menu.js";
import type { MenuModel } from "../src/builder/build.js";
import type { Manifest } from "../src/manifest/schema.js";

const fx = (name: string) => fileURLToPath(new URL(`./fixtures/${name}`, import.meta.url));

function modelFor(root: string): MenuModel {
  return buildMenuModel(scanProject(root), loadDefinition(root));
}

function tempProject(pkg: object, files: Record<string, string> = {}): string {
  const root = mkdtempSync(join(tmpdir(), "navig-plugin-"));
  writeFileSync(join(root, "package.json"), JSON.stringify(pkg, null, 2) + "\n", "utf8");
  for (const [rel, content] of Object.entries(files)) {
    const abs = join(root, rel);
    mkdirSync(join(abs, ".."), { recursive: true });
    writeFileSync(abs, content, "utf8");
  }
  return root;
}

function renderView(root: string, view: string, model?: MenuModel): string {
  const m: Manifest = scanProject(root);
  const built = model ?? buildMenuModel(m, loadDefinition(root));
  const draft = new Map<string, boolean | string>();
  for (const p of built.loadedPlugins ?? []) draft.set(p.id, p.active);
  for (const s of built.pluginSettings ?? []) draft.set(`${s.pluginId}::${s.key}`, s.value);
  return renderMenuSnapshot(
    built,
    { root, theme: createTheme({ plain: true }), mode: "local", git: { isRepo: false, dirty: 0 }, manifest: m, toolVersion: "9.9.9" },
    { width: 84, height: 44 },
    { selected: 0, offset: 0, filter: "", view, settingsIndex: 0, customizerIndex: 0, customizerOffset: 0, pluginIndex: 0, pluginOffset: 0, category: null, pickerIndex: 0 } as never,
    undefined,
    undefined,
    undefined,
    undefined,
    draft,
  );
}

describe("store plugin (built-in, skeleton)", () => {
  it("auto-activates on a store project + shows a banner stat line", () => {
    const model = modelFor(fx("store-project"));
    const store = model.loadedPlugins?.find((p) => p.id === "store");
    expect(store?.active).toBe(true);
    expect(store?.auto).toBe(true);
    expect(model.bannerLines?.some((l) => /Atlas/.test(l.text) && /downloads/.test(l.text))).toBe(true);
  });

  it("claims store:*/steam:* scripts into a STORE section", () => {
    const model = modelFor(fx("store-project"));
    expect(model.groups.some((g) => g.group === "Store")).toBe(true);
    expect(model.allScripts.find((a) => a.id === "store:publish")?.group).toBe("Store");
    expect(model.allScripts.find((a) => a.id === "steam:build")?.group).toBe("Store");
    // plugin internal actions are appended
    expect(model.allScripts.some((a) => a.source.startsWith("plugin:internal:store:"))).toBe(true);
  });

  it("does NOT activate on an unrelated project (no banner line)", () => {
    const model = modelFor(fx("pnpm-workspace"));
    expect(model.loadedPlugins?.find((p) => p.id === "store")?.active).toBe(false);
    expect(model.bannerLines).toBeUndefined();
  });
});

describe("plugin activation state", () => {
  it("enabled:false beats a matching detect", () => {
    const root = tempProject(
      { name: "s", scripts: { "store:publish": "x" } },
      { ".navig/menu.json": JSON.stringify({ plugins: { store: { enabled: false } } }) },
    );
    try {
      const model = modelFor(root);
      expect(model.loadedPlugins?.find((p) => p.id === "store")?.active).toBe(false);
      expect(model.groups.some((g) => g.group === "Store")).toBe(false);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("enabled:true activates a plugin whose detect never matches (example)", () => {
    const root = tempProject(
      { name: "s", scripts: { dev: "vite" } },
      { ".navig/menu.json": JSON.stringify({ plugins: { example: { enabled: true } } }) },
    );
    try {
      const model = modelFor(root);
      const ex = model.loadedPlugins?.find((p) => p.id === "example");
      expect(ex?.active).toBe(true);
      expect(ex?.auto).toBe(false);
      expect(model.bannerPhrases?.some((p) => /hello from a plugin/.test(p))).toBe(true);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("round-trips activation to .navig/menu.json", () => {
    const root = tempProject({ name: "s", scripts: { "store:publish": "x" } });
    try {
      expect(modelFor(root).loadedPlugins?.find((p) => p.id === "store")?.active).toBe(true);
      savePluginState(root, "store", { enabled: false }, scanProject(root));
      expect(modelFor(root).loadedPlugins?.find((p) => p.id === "store")?.active).toBe(false);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

describe("local plugins (declarative + programmatic)", () => {
  it("loads json + mjs, folds contributions, isolates broken ones", async () => {
    const root = fx("local-plugin");
    const plugins = await loadPlugins(root, {});
    const model = buildMenuModel(scanProject(root), loadDefinition(root), plugins);

    expect(model.bannerPhrases).toEqual(expect.arrayContaining(["hello from json", "hello from mjs"]));
    expect(model.textOverrides?.tagline).toBe("team console");
    expect(model.groups.some((g) => g.group === "Team")).toBe(true);
    // broken.mjs + bad.json are isolated as warnings, never crashes
    expect(model.warnings.some((w) => w.code === "plugin_error")).toBe(true);
    expect(model.loadedPlugins?.some((p) => p.id === "dyn" && p.active)).toBe(true);
  });
});

describe("ports plugin", () => {
  it("auto-activates on a project that serves on a port", () => {
    const model = modelFor(fx("next-stripe-cf"));
    expect(model.loadedPlugins?.find((p) => p.id === "ports")?.active).toBe(true);
    expect(model.groups.some((g) => g.group === "Ports")).toBe(true);
    expect(model.allScripts.some((a) => a.source === "plugin:internal:ports:kill")).toBe(true);
  });

  it("stays off for a project with no detected endpoints", () => {
    const model = modelFor(fx("pnpm-workspace"));
    expect(model.loadedPlugins?.find((p) => p.id === "ports")?.active).toBe(false);
  });

  it("parses netstat / lsof / tasklist + ranges", () => {
    const netstat = [
      " Active Connections",
      "  Proto  Local Address     Foreign Address   State       PID",
      "  TCP    0.0.0.0:3000      0.0.0.0:0         LISTENING   12345",
      "  TCP    127.0.0.1:5173    0.0.0.0:0         LISTENING   6789",
      "  TCP    0.0.0.0:445       0.0.0.0:0         ESTABLISHED 4",
    ].join("\n");
    expect(parseNetstat(netstat)).toEqual([
      { port: 3000, pid: 12345 },
      { port: 5173, pid: 6789 },
    ]);

    const lsof = [
      "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME",
      "node    111 me 23u IPv4 0x1 0t0 TCP *:3000 (LISTEN)",
      "Google  222 me 40u IPv6 0x2 0t0 TCP [::1]:5173 (LISTEN)",
    ].join("\n");
    expect(parseLsof(lsof)).toEqual([
      { port: 3000, pid: 111, name: "node" },
      { port: 5173, pid: 222, name: "Google" },
    ]);

    expect(parseTasklist('"node.exe","12345","Console","1","50,000 K"')).toEqual({ 12345: "node.exe" });
    expect(parseRange("8000-8999", [3000, 3999])).toEqual([8000, 8999]);
    expect(parseRange("nope", [3000, 3999])).toEqual([3000, 3999]);
    expect(parseRange("9000-8000", [1, 2])).toEqual([8000, 9000]);
  });

  it("finds an actually-free port in a range", async () => {
    const port = await findFreePort(20000, 20050);
    expect(port).toBeGreaterThanOrEqual(20000);
    expect(port).toBeLessThanOrEqual(20050);
  });

  it("derives the project's own ports from its endpoints (auto-free)", () => {
    expect(
      endpointPorts({ endpoints: [{ url: "http://localhost:3000" }, { url: "https://localhost" }, { url: "http://localhost:8787/" }] }),
    ).toEqual([443, 3000, 8787]);
  });

  it("upserts PORT= into an env file without touching other values (auto-change)", () => {
    expect(upsertEnvPort("FOO=1\nPORT=3000\nBAR=2", 4000)).toBe("FOO=1\nPORT=4000\nBAR=2");
    expect(upsertEnvPort("FOO=1", 4000)).toBe("FOO=1\nPORT=4000\n");
    expect(upsertEnvPort("", 4000)).toBe("PORT=4000\n");
  });
});

describe("plugin views + text override", () => {
  it("banner reflects a plugin text override", () => {
    expect(renderView(fx("local-plugin"), "menu")).toContain("team console");
  });

  it("about view credits the site + author and lists plugins", () => {
    const out = renderView(fx("store-project"), "about");
    expect(out).toContain("navig-menu");
    expect(out).toContain("navig.run");
    expect(out).toContain("github.com/miztizm");
    expect(out).not.toContain("Cybesis Studio"); // studio row removed
    expect(out).toContain("store");
  });

  it("plugin manager lists plugins with checkboxes", () => {
    const out = renderView(fx("store-project"), "plugins");
    expect(out).toContain("PLUGINS");
    expect(out).toContain("store");
    expect(out).toContain("[x]");
  });
});
