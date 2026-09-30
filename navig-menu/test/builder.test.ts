import { describe, it, expect } from "vitest";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { scanProject, loadDefinition, buildMenuModel } from "../src/builder/build.js";
import { resolveCanonical } from "../src/builder/canonical.js";
import { riskFor } from "../src/builder/risk.js";
import { tokenize } from "../src/builder/merge.js";
import { generateMenuDefinition } from "../src/builder/generate.js";
import { groupFor, sectionMeta } from "../src/builder/classify.js";
import { readMenuHistory, recordMenuAction, resolveRecentActions } from "../src/ui/history.js";
import { ensureMenuScript } from "../src/commands/index.js";
import { createTheme } from "../src/ui/theme.js";
import { renderMenuSnapshot } from "../src/ui/menu.js";
import { resolveSettings, saveSettings, saveHidden } from "../src/ui/settings.js";
import type { Action } from "../src/manifest/schema.js";

const fx = (name: string) => fileURLToPath(new URL(`./fixtures/${name}`, import.meta.url));

describe("package manager detection", () => {
  it("prefers the packageManager field (explicit)", () => {
    const m = scanProject(fx("pnpm-workspace"));
    expect(m.packageManager.value).toBe("pnpm");
    expect(m.packageManager.confidence).toBe("explicit");
  });

  it("detects from a lockfile when no field", () => {
    const m = scanProject(fx("npm-single"));
    expect(m.packageManager.value).toBe("npm");
    expect(m.packageManager.confidence).toBe("detected");
  });

  it("warns + drops to inferred on conflicting lockfiles", () => {
    const m = scanProject(fx("conflicting-lockfiles"));
    expect(m.packageManager.value).toBe("pnpm"); // priority order
    expect(m.packageManager.confidence).toBe("inferred");
    expect(m.warnings.some((w) => w.code === "multiple_lockfiles")).toBe(true);
  });
});

describe("workspace detection", () => {
  it("resolves pnpm workspace packages", () => {
    const m = scanProject(fx("pnpm-workspace"));
    expect(m.workspace.kind).toBe("pnpm");
    expect(m.workspace.packages.some((p) => p.includes("packages/ui"))).toBe(true);
  });
});

describe("framework + service detection", () => {
  it("finds next, stripe (with webhook route), cloudflare, prisma", () => {
    const m = scanProject(fx("next-stripe-cf"));
    expect(m.frameworks.map((f) => f.id)).toContain("next");
    const stripe = m.services.find((s) => s.id === "stripe");
    expect(stripe?.confidence).toBe("detected"); // webhook route raises confidence
    expect(stripe?.evidence.some((e) => e.kind === "route")).toBe(true);
    expect(m.services.map((s) => s.id)).toEqual(expect.arrayContaining(["cloudflare", "prisma"]));
  });

  it("classifies risk: deploy=dangerous, build=confirm, dev=safe, db:reset=dangerous", () => {
    const m = scanProject(fx("next-stripe-cf"));
    const byId = Object.fromEntries(m.actions.map((a) => [a.id, a]));
    expect(byId["dev"]?.risk).toBe("safe");
    expect(byId["build"]?.risk).toBe("confirm");
    expect(byId["deploy"]?.risk).toBe("dangerous");
    expect(byId["db:reset"]?.risk).toBe("dangerous");
    expect(byId["dev"]?.longRunning).toBe(true);
  });
});

describe("secret safety", () => {
  it("never leaks .env values into the manifest", () => {
    const m = scanProject(fx("secret-safety"));
    const json = JSON.stringify(m);
    expect(json).not.toContain("sk_live_THIS_VALUE_MUST_NEVER_LEAK");
    expect(json).not.toContain("p4ssw0rd_SECRET");
  });
});

describe("override merge", () => {
  it("applies title/accent, overrides command, hides, adds extras", () => {
    const root = fx("override-merge");
    const model = buildMenuModel(scanProject(root), loadDefinition(root));
    expect(model.title).toBe("Override Demo");
    expect(model.accent).toBe("magenta");

    const dev = model.allScripts.find((a) => a.canonical === "dev");
    expect(dev?.launcher).toBe("pnpm");
    expect(dev?.label).toBe("Dev (overridden)");
    expect(dev?.confidence).toBe("explicit");

    expect(model.allScripts.some((a) => a.id === "prepare")).toBe(false); // hidden
    expect(model.allScripts.some((a) => a.id === "studio")).toBe(true); // extra
  });
});

describe("menu generator", () => {
  it("generates a rich menu definition with pro UI defaults", () => {
    const manifest = scanProject(fx("pnpm-workspace"));
    const { definition, audit } = generateMenuDefinition(manifest);
    const dev = definition.actions?.dev;

    expect(definition.title).toBe("pnpm-monorepo");
    expect(definition.ui?.recentLimit).toBe(5);
    expect(definition.ui?.showCommandHints).toBe(true);
    expect(typeof dev).toBe("object");
    if (typeof dev === "object") {
      expect(dev.cmd).toBe("pnpm run dev");
      expect(dev.longRunning).toBe(true);
      expect(dev.description).toContain("development");
    }
    expect(audit.mapped).toEqual(expect.arrayContaining(["dev -> dev", "build -> build", "test -> test"]));
  });

  it("preserves the full ui block (banner/glyphs/accent) when regenerating", () => {
    const manifest = scanProject(fx("pnpm-workspace"));
    const existing = {
      accent: "purple" as const,
      ui: { glyphs: "ascii" as const, banner: { date: false, endpoints: false } },
    };
    const { definition } = generateMenuDefinition(manifest, existing);
    expect(definition.accent).toBe("purple");
    expect(definition.ui?.glyphs).toBe("ascii");
    expect(definition.ui?.banner?.date).toBe(false);
    expect(definition.ui?.recentLimit).toBe(5); // core default still filled
  });

  it("preserves existing human edits while adding missing generated actions", () => {
    const manifest = scanProject(fx("override-merge"));
    const existing = loadDefinition(fx("override-merge")).def;
    const { definition, audit } = generateMenuDefinition(manifest, existing);

    const dev = definition.actions?.dev;
    const build = definition.actions?.build;
    expect(typeof dev).toBe("object");
    if (typeof dev === "object") expect(dev.label).toBe("Dev (overridden)");
    expect(typeof build).toBe("object");
    expect(definition.hide).toContain("prepare");
    expect(definition.extra?.[0]?.id).toBe("studio");
    expect(audit.preserved).toContain("dev");
  });

  it("adds a menu package script without changing existing scripts", async () => {
    const root = mkdtempSync(join(tmpdir(), "navig-menu-script-"));
    try {
      writeFileSync(
        join(root, "package.json"),
        JSON.stringify({ scripts: { dev: "vite" } }, null, 2) + "\n",
        "utf8",
      );
      const result = await ensureMenuScript(root, createTheme({ plain: true }), { assumeYes: true });
      const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8")) as {
        scripts: Record<string, string>;
      };

      expect(result).toBe("added");
      expect(pkg.scripts.dev).toBe("vite");
      expect(pkg.scripts.menu).toBe("npx --yes navig-menu");
      expect(pkg.scripts["menu:navig"]).toBe("npx --yes navig-menu");
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("keeps an existing menu script and adds a generated-menu alias", async () => {
    const root = mkdtempSync(join(tmpdir(), "navig-menu-script-existing-"));
    try {
      writeFileSync(
        join(root, "package.json"),
        JSON.stringify({ scripts: { menu: "node scripts/menu.mjs" } }, null, 2) + "\n",
        "utf8",
      );
      const result = await ensureMenuScript(root, createTheme({ plain: true }), { assumeYes: true });
      const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8")) as {
        scripts: Record<string, string>;
      };

      expect(result).toBe("alias-added");
      expect(pkg.scripts.menu).toBe("node scripts/menu.mjs");
      expect(pkg.scripts["menu:navig"]).toBe("npx --yes navig-menu");
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

describe("graceful degradation", () => {
  it("handles a directory with no package.json", () => {
    const m = scanProject(fx("no-package-json"));
    expect(m.packageManager.value).toBe("none");
    expect(m.actions.length).toBe(0);
  });
});

describe("unit: canonical + risk + tokenize", () => {
  it("resolves canonical names", () => {
    expect(resolveCanonical("dev")).toBe("dev");
    expect(resolveCanonical("serve")).toBe("dev");
    expect(resolveCanonical("compile")).toBe("build");
    expect(resolveCanonical("format")).toBe("lint:fix");
    expect(resolveCanonical("totally-custom")).toBeUndefined();
  });

  it("escalates risk on dangerous keywords", () => {
    expect(riskFor("test", "vitest run", "test")).toBe("safe");
    expect(riskFor("build", "next build", "build")).toBe("confirm");
    expect(riskFor("nuke", "rm -rf dist", undefined)).toBe("dangerous");
  });

  it("tokenizes commands without a shell", () => {
    expect(tokenize("pnpm run dev")).toEqual(["pnpm", "run", "dev"]);
    expect(tokenize('node -e "console.log(1)"')).toEqual(["node", "-e", "console.log(1)"]);
  });
});

describe("menu history", () => {
  it("dedupes recent actions and resolves only actions still present", () => {
    const root = mkdtempSync(join(tmpdir(), "navig-menu-history-"));
    try {
      const dev = actionFixture("dev", "Dev", "dev");
      const test = actionFixture("test", "Test", "test");
      recordMenuAction(root, dev, 5);
      recordMenuAction(root, test, 5);
      recordMenuAction(root, dev, 5);

      const history = readMenuHistory(root);
      expect(history.recent.map((item) => item.id)).toEqual(["dev", "test"]);
      expect(history.recent[0]?.count).toBe(2);
      expect(resolveRecentActions(root, [test], 5).map((action) => action.id)).toEqual(["test"]);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

describe("menu rendering", () => {
  it("renders a static Schema-style menu preview", () => {
    const root = fx("pnpm-workspace");
    const manifest = scanProject(root);
    const model = buildMenuModel(manifest, loadDefinition(root));
    const snapshot = renderMenuSnapshot(
      model,
      {
        root,
        theme: createTheme({ plain: true }),
        mode: "local",
        git: { isRepo: false, dirty: 0 },
        manifest,
      },
      { width: 72, height: 44 },
    );

    expect(snapshot).toContain("developer console");
    expect(snapshot).toContain("What would you like to do?");
    expect(snapshot).toContain("DEVELOPMENT");
    expect(snapshot).toContain("$ pnpm run dev");
    expect(snapshot).toContain("settings");
    expect(snapshot).toContain("UTILITIES");
  });
});

describe("menu panels", () => {
  const renderView = (view: string) => {
    const root = fx("pnpm-workspace");
    const manifest = scanProject(root);
    const model = buildMenuModel(manifest, loadDefinition(root));
    const settings = resolveSettings(model.ui, model.accent);
    return renderMenuSnapshot(
      model,
      { root, theme: createTheme({ plain: true }), mode: "local", git: { isRepo: false, dirty: 0 }, manifest, settings },
      { width: 80, height: 40 },
      { selected: 0, offset: 0, filter: "", view, settingsIndex: 0, customizerIndex: 0, customizerOffset: 0 } as never,
      undefined,
      settings,
    );
  };

  it("renders the settings panel (with live preview, grouped headings + folded actions)", () => {
    const out = renderView("settings");
    expect(out).toContain("preview"); // live sample of the draft
    expect(out).toContain("Dev"); // sample row
    expect(out).toContain("SETTINGS");
    expect(out).toContain("Look");
    expect(out).toContain("Accent");
    expect(out).toContain("Layout");
  });

  it("renders the doctor panel", () => {
    const out = renderView("details");
    expect(out).toContain("DOCTOR");
    expect(out).toContain("ENDPOINTS");
    expect(out).toContain("MENU CONTRACT");
  });

  it("renders the customizer panel with checkboxes", () => {
    const out = renderView("customizer");
    expect(out).toContain("CUSTOMIZE MENU");
    expect(out).toContain("[x]"); // plain checkbox for a visible item
  });
});

describe("layout modes", () => {
  const root = fx("pnpm-workspace");
  const render = (layout: "flat" | "list" | "categories" | "tree", category: string | null, bannerStyle: "cosmic" | "console" = "cosmic") => {
    const manifest = scanProject(root);
    const model = buildMenuModel(manifest, loadDefinition(root));
    const settings = { ...resolveSettings(model.ui, model.accent), layout, bannerStyle };
    return renderMenuSnapshot(
      model,
      { root, theme: createTheme({ plain: true }), mode: "local", git: { isRepo: false, dirty: 0 }, manifest, settings, toolVersion: "9.9.9" },
      { width: 80, height: 40 },
      { selected: 0, offset: 0, filter: "", view: "menu", settingsIndex: 0, customizerIndex: 0, customizerOffset: 0, pluginIndex: 0, pluginOffset: 0, category, pickerIndex: 0 } as never,
      undefined,
      settings,
    );
  };

  it("flat layout shows every command on one page", () => {
    const out = render("flat", null);
    expect(out).toContain("DEVELOPMENT");
    expect(out).toContain("$ pnpm run dev");
  });

  it("categories layout shows a picker (cards, no commands)", () => {
    const out = render("categories", null);
    expect(out).toContain("DEVELOPMENT");
    expect(out).toContain("enter open");
    expect(out).not.toContain("$ pnpm run dev");
  });

  it("entering a category reveals its commands + a back hint", () => {
    const out = render("categories", "Development");
    expect(out).toContain("$ pnpm run dev");
    expect(out).toContain("esc back");
  });

  it("tree layout renders filesystem connectors", () => {
    const out = render("tree", null);
    expect(out).toContain("DEVELOPMENT");
    expect(out).toContain("Dev");
    expect(out).toMatch(/\+-|`-/); // ascii tree connectors (unicode: ├─ └─)
  });

  it("list layout streams every command with no section headers", () => {
    const out = render("list", null);
    expect(out).toContain("$ pnpm run dev"); // commands present
    expect(out).not.toContain("DEVELOPMENT"); // but no section titles
    expect(out).not.toMatch(/\+-|`-/); // and no tree connectors
  });

  it("console banner style shows a compact status line with a command count", () => {
    const out = render("flat", null, "console");
    expect(out).toContain("commands");
    expect(out).toMatch(/-{6,}|─{6,}/); // banner rule (ascii or unicode)
    expect(out).toContain("|"); // open box left rail (ascii)
  });

  it("tree layout nests namespaced scripts into sub-folders", () => {
    const root = mkdtempSync(join(tmpdir(), "navig-menu-tree-"));
    try {
      writeFileSync(
        join(root, "package.json"),
        JSON.stringify({ scripts: { "db:migrate": "prisma migrate", "db:reset": "prisma reset" } }, null, 2) + "\n",
        "utf8",
      );
      const model = buildMenuModel(scanProject(root), loadDefinition(root));
      const settings = { ...resolveSettings(model.ui, model.accent), layout: "tree" as const };
      const out = renderMenuSnapshot(
        model,
        { root, theme: createTheme({ plain: true }), mode: "local", git: { isRepo: false, dirty: 0 }, manifest: scanProject(root), settings, toolVersion: "9.9.9" },
        { width: 90, height: 40 },
        { selected: 0, offset: 0, filter: "", view: "menu", settingsIndex: 0, customizerIndex: 0, customizerOffset: 0, pluginIndex: 0, pluginOffset: 0, category: null, pickerIndex: 0 } as never,
        undefined,
        settings,
      );
      expect(out).toContain("db"); // the shared namespace becomes a folder node
      expect(out).toMatch(/\+-|`-/); // nested connectors
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

describe("environment detection", () => {
  const detect = (files: Record<string, string>, pkg: object = {}) => {
    const root = mkdtempSync(join(tmpdir(), "navig-menu-env-"));
    try {
      writeFileSync(join(root, "package.json"), JSON.stringify(pkg, null, 2) + "\n", "utf8");
      for (const [rel, content] of Object.entries(files)) {
        const abs = join(root, rel);
        mkdirSync(join(abs, ".."), { recursive: true });
        writeFileSync(abs, content, "utf8");
      }
      return scanProject(root).frameworks.map((f) => f.id);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  };

  it("detects a Tauri desktop app", () => {
    expect(detect({ "src-tauri/tauri.conf.json": "{}" }, { dependencies: { "@tauri-apps/api": "^2" } })).toContain("tauri");
  });

  it("detects an Apple / Xcode project", () => {
    expect(detect({ "ios/App.xcodeproj/project.pbxproj": "// pbx", "ios/Podfile": "platform :ios" })).toEqual(
      expect.arrayContaining(["apple"]),
    );
  });

  it("detects Flutter, Electron, Swift, Android, and Go", () => {
    expect(detect({ "pubspec.yaml": "name: app" })).toContain("flutter");
    expect(detect({}, { dependencies: { electron: "^30" } })).toContain("electron");
    expect(detect({ "Package.swift": "// swift" })).toContain("swift");
    expect(detect({ "app/src/main/AndroidManifest.xml": "<manifest/>" })).toContain("android");
    expect(detect({ "go.mod": "module x" })).toContain("go");
  });
});

describe("curated overrides + groups", () => {
  it("relabels/regroups a detected script and orders custom sections first", () => {
    const root = mkdtempSync(join(tmpdir(), "navig-menu-curated-"));
    try {
      writeFileSync(join(root, "package.json"), JSON.stringify({ scripts: { dev: "vite", test: "vitest" } }, null, 2) + "\n", "utf8");
      mkdirSync(join(root, ".navig"), { recursive: true });
      writeFileSync(
        join(root, ".navig", "menu.json"),
        JSON.stringify({
          groups: [{ name: "RUN", icon: "✦", tone: "blue" }],
          overrides: { dev: { label: "Serve", description: "local server", group: "RUN" } },
        }),
        "utf8",
      );
      const model = buildMenuModel(scanProject(root), loadDefinition(root));
      const dev = model.allScripts.find((a) => a.id === "dev");
      expect(dev?.label).toBe("Serve");
      expect(dev?.group).toBe("RUN");
      expect(dev?.description).toBe("local server");
      expect(model.groups[0]?.group).toBe("RUN"); // custom groups ordered first
      expect(sectionMeta("RUN").title).toBe("RUN"); // meta registered from the definition
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

describe("section classification", () => {
  it("routes namespaced scripts to the right rail", () => {
    expect(groupFor("webapp:dev")).toBe("Development");
    expect(groupFor("api:migration:run")).toBe("Database");
    expect(groupFor("seed:demo:local")).toBe("Database");
    expect(groupFor("i18n:extract")).toBe("i18n");
    expect(groupFor("desktop:typecheck")).toBe("Desktop & Store");
    expect(groupFor("store:publish")).toBe("Desktop & Store");
    expect(groupFor("deploy:api")).toBe("Deploy");
    expect(groupFor("webapp:build")).toBe("Build");
    expect(groupFor("test:e2e")).toBe("Quality");
    expect(groupFor("totally-custom")).toBe("Misc");
  });

  it("categorizes asset / cert / version / bench scripts (real-project coverage)", () => {
    expect(groupFor("assets:validate")).toBe("Assets");
    expect(groupFor("generate-icons")).toBe("Assets");
    expect(groupFor("capture-screenshots")).toBe("Assets");
    expect(groupFor("theme:generate")).toBe("Assets");
    expect(groupFor("wack:aurora")).toBe("Desktop & Store");
    expect(groupFor("store:install:aurora")).toBe("Desktop & Store");
    expect(groupFor("version-sync")).toBe("Operations");
    expect(groupFor("benchmark")).toBe("Quality");
  });

  it("exposes presentation metadata for every group", () => {
    expect(sectionMeta("Database").title).toBe("DATABASE");
    expect(sectionMeta("Desktop & Store").title).toBe("DESKTOP / STORE");
    expect(sectionMeta("Utilities").title).toBe("UTILITIES"); // the synthetic meta rail
    expect(sectionMeta("Misc").title).toBe("MISC");
  });
});

describe("endpoint detection", () => {
  it("infers a local web URL from a framework dev script", () => {
    const m = scanProject(fx("next-stripe-cf"));
    const web = m.endpoints.find((e) => e.kind === "web");
    expect(web?.url).toContain("3000");
  });

  it("never includes separators, empties, or the menu's own launcher scripts", () => {
    const root = mkdtempSync(join(tmpdir(), "navig-menu-sep-"));
    try {
      writeFileSync(
        join(root, "package.json"),
        JSON.stringify(
          {
            scripts: {
              "——— DEV ———": "",
              dev: "vite",
              "  ": "noop",
              menu: "node scripts/menu.mjs",
              "menu:navig": "npx navig-menu",
            },
          },
          null,
          2,
        ) + "\n",
        "utf8",
      );
      const m = scanProject(root);
      expect(m.actions.map((a) => a.id)).toEqual(["dev"]);
      expect(Object.keys(m.scripts)).toEqual(["dev"]);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

describe("settings", () => {
  it("resolves built-in defaults when nothing is set (ascii + flat + categorized)", () => {
    const s = resolveSettings(undefined, undefined);
    expect(s.accent).toBe("cyan");
    expect(s.glyphs).toBe("ascii");
    expect(s.layout).toBe("flat");
    expect(s.showNavigSection).toBe(true);
    expect(s.banner.endpoints).toBe(true);
    expect(s.banner.plugins).toBe(true);
  });

  it("lets the project override defaults, accent wins from top-level", () => {
    const s = resolveSettings({ showDescriptions: false, banner: { date: false } }, "purple");
    expect(s.accent).toBe("purple");
    expect(s.showDescriptions).toBe(false);
    expect(s.banner.date).toBe(false);
    expect(s.banner.endpoints).toBe(true); // untouched default
  });

  it("persists to .navig/menu.json and round-trips", () => {
    const root = mkdtempSync(join(tmpdir(), "navig-menu-settings-"));
    try {
      writeFileSync(
        join(root, "package.json"),
        JSON.stringify({ name: "demo", scripts: { dev: "vite" } }, null, 2) + "\n",
        "utf8",
      );
      const manifest = scanProject(root);
      saveSettings(root, { ...resolveSettings(undefined, undefined), accent: "magenta" }, manifest);
      const written = JSON.parse(readFileSync(join(root, ".navig", "menu.json"), "utf8")) as {
        accent: string;
        ui: { accent: string; banner: Record<string, unknown> };
      };
      expect(written.accent).toBe("magenta");
      expect(written.ui.accent).toBe("magenta");
      expect(written.ui.banner).toBeTypeOf("object");
      expect(resolveSettings(written.ui, written.accent).accent).toBe("magenta");
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("customizer: hiding all items in a category drops the category", () => {
    const root = mkdtempSync(join(tmpdir(), "navig-menu-hide-"));
    try {
      writeFileSync(
        join(root, "package.json"),
        JSON.stringify({ name: "demo", scripts: { dev: "vite", build: "vite build", test: "vitest" } }, null, 2) + "\n",
        "utf8",
      );
      const manifest = scanProject(root);
      saveHidden(root, ["build"], manifest); // BUILD has a single item → category vanishes
      const model = buildMenuModel(scanProject(root), loadDefinition(root));
      expect(model.groups.some((g) => g.group === "Build")).toBe(false);
      expect(model.allScripts.some((a) => a.id === "build")).toBe(false);
      expect(model.allScripts.some((a) => a.id === "dev")).toBe(true);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

function actionFixture(id: string, label: string, canonical?: string): Action {
  return {
    id,
    canonical,
    label,
    group: "Launch",
    launcher: "pnpm",
    argv: ["run", id],
    cwd: ".",
    risk: "safe",
    longRunning: false,
    confidence: "explicit",
    evidence: [],
    source: "test",
  };
}
