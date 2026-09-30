import { describe, it, expect } from "vitest";
import {
  importCatalog,
  hasShellOperators,
  parseCwdCommand,
  NavigCatalogSchema,
  type NavigCatalog,
} from "../src/builder/import-catalog.js";
import type { Manifest, MenuDefinition } from "../src/manifest/schema.js";

/** Minimal manifest — importCatalog only reads `.scripts` for demotion. */
const manifestWith = (scripts: Record<string, string>): Manifest =>
  ({ scripts } as unknown as Manifest);

const catalog = (categories: NavigCatalog["categories"]): NavigCatalog => ({ version: 1, categories });

describe("import-catalog: item translation", () => {
  it("maps exec/cwd/desc/danger/label onto an extra action, grouped by the section label", () => {
    const { definition, audit } = importCatalog(
      catalog([
        {
          key: "deploy",
          icon: "🚀",
          label: "Deploy",
          items: [
            { id: "www:deploy", label: "Landing · deploy", desc: "ship the site", exec: "npm run deploy", cwd: "web/www", danger: true },
          ],
        },
      ]),
    );
    const e = definition.extra?.find((x) => x.id === "www:deploy");
    expect(e).toBeDefined();
    expect(e!.cmd).toBe("npm run deploy");
    expect(e!.cwd).toBe("web/www");
    expect(e!.description).toBe("ship the site");
    expect(e!.risk).toBe("dangerous");
    expect(e!.group).toBe("DEPLOY");
    expect(audit.imported).toContain("www:deploy");
    expect(definition.groups?.[0]?.name).toBe("DEPLOY");
  });

  it("dedups a repeated id across categories — first import wins, duplicate audited", () => {
    const { definition, audit } = importCatalog(
      catalog([
        { key: "git", label: "Git", items: [{ id: "status", label: "Git status", exec: "git status" }] },
        { key: "quickstart", label: "Quickstart", items: [{ id: "status", label: "Server status", exec: "systemctl status" }] },
      ]),
    );
    const matches = (definition.extra ?? []).filter((x) => x.id === "status");
    expect(matches).toHaveLength(1); // no doubled row
    expect(matches[0]!.cmd).toBe("git status"); // first wins
    expect(audit.skippedDuplicate).toContain("status");
  });

  it("infers longRunning for dev-category and gateway commands", () => {
    const { definition } = importCatalog(
      catalog([
        {
          key: "dev",
          label: "Dev servers",
          items: [
            { id: "deck:dev", label: "Deck dev", exec: "npm run dev", cwd: "apps/deck" },
            { id: "core:gateway", label: "Gateway", exec: "navig gateway start", cwd: "core" },
          ],
        },
      ]),
    );
    expect(definition.extra?.find((x) => x.id === "deck:dev")?.longRunning).toBe(true);
    expect(definition.extra?.find((x) => x.id === "core:gateway")?.longRunning).toBe(true);
  });

  it("skips shell-operator, interactive (special), and empty items — and audits each", () => {
    const { definition, audit } = importCatalog(
      catalog([
        {
          label: "Mixed",
          items: [
            { id: "ok", label: "OK", exec: "git status -s" },
            { id: "chain", label: "Chain", exec: "npm run a && npm run b" },
            { id: "mint", label: "Mint", special: "mint-license" },
            { id: "blank", label: "Blank" },
          ],
        },
      ]),
    );
    const ids = (definition.extra ?? []).map((e) => e.id);
    expect(ids).toEqual(["ok"]);
    expect(audit.skippedShell).toEqual(["chain"]);
    expect(audit.skippedSpecial).toEqual(["mint"]);
    expect(audit.skippedEmpty).toEqual(["blank"]);
  });
});

describe("import-catalog: dedup / demote", () => {
  it("demotes a detected root wrapper that runs the same command (exact match)", () => {
    const { audit } = importCatalog(
      catalog([{ key: "dev", label: "Dev", items: [{ id: "deck:dev", label: "Deck", exec: "npm run dev", cwd: "apps/deck" }] }]),
      {},
      manifestWith({ "dev:deck": "cd apps/deck && npm run dev", other: "vite" }),
    );
    expect(audit.demoted).toContain("dev:deck");
    expect(audit.demoted).not.toContain("other");
  });

  it("demotes a stale root script by intent (same dir + canonical) even when the command drifted", () => {
    const { audit } = importCatalog(
      catalog([{ key: "dev", label: "Dev", items: [{ id: "os:dev", label: "OS dev", exec: "bun run electron:dev", cwd: "apps/os" }] }]),
      {},
      manifestWith({ "dev:os": "cd apps/os && npx next dev -p 7001" }),
    );
    expect(audit.demoted).toContain("dev:os");
  });

  it("does not demote a different command in a different package", () => {
    const { audit } = importCatalog(
      catalog([{ key: "dev", label: "Dev", items: [{ id: "os:dev", label: "OS dev", exec: "bun run electron:dev", cwd: "apps/os" }] }]),
      {},
      manifestWith({ "dev:deck": "cd apps/deck && npm run dev" }),
    );
    expect(audit.demoted).not.toContain("dev:deck");
  });
});

describe("import-catalog: merge & idempotency", () => {
  const cat = catalog([
    { key: "dev", label: "Dev", items: [{ id: "deck:dev", label: "Deck", exec: "npm run dev", cwd: "apps/deck" }] },
  ]);

  it("is idempotent — re-import yields the same single extra (no duplicates)", () => {
    const first = importCatalog(cat).definition;
    const second = importCatalog(cat, first).definition;
    expect(second.extra?.filter((e) => e.id === "deck:dev")).toHaveLength(1);
  });

  it("preserves human-authored extras the catalog does not own", () => {
    const existing: MenuDefinition = { extra: [{ id: "my:thing", label: "Mine", cmd: "echo hi", group: "MISC" }] };
    const { definition } = importCatalog(cat, existing);
    expect(definition.extra?.some((e) => e.id === "my:thing")).toBe(true);
    expect(definition.extra?.some((e) => e.id === "deck:dev")).toBe(true);
  });

  it("prunes an inert override that now targets a demoted script, keeps a live one", () => {
    const existing: MenuDefinition = {
      overrides: {
        "dev:deck": { label: "stale" }, // will be demoted → inert → pruned
        keep: { label: "Keep me" }, // still a live detected script → kept
      },
    };
    const { definition } = importCatalog(cat, existing, manifestWith({ "dev:deck": "cd apps/deck && npm run dev", keep: "vite" }));
    expect(definition.overrides?.["dev:deck"]).toBeUndefined();
    expect(definition.overrides?.keep?.label).toBe("Keep me");
  });
});

describe("import-catalog: helpers", () => {
  it("hasShellOperators flags chaining/pipes/redirects but not -- or single flags", () => {
    expect(hasShellOperators("npm run a && npm run b")).toBe(true);
    expect(hasShellOperators("a | b")).toBe(true);
    expect(hasShellOperators("echo x > f")).toBe(true);
    expect(hasShellOperators("npm run build-all -- vscode-only")).toBe(false);
    expect(hasShellOperators("pwsh -NoProfile -File x.ps1")).toBe(false);
  });

  it("parseCwdCommand splits a leading `cd <dir> &&`", () => {
    expect(parseCwdCommand("cd apps/deck && npm run dev")).toEqual({ cwd: "apps/deck", cmd: "npm run dev" });
    expect(parseCwdCommand("npm run dev")).toEqual({ cwd: ".", cmd: "npm run dev" });
  });

  it("NavigCatalogSchema accepts the reference shape", () => {
    expect(() => NavigCatalogSchema.parse({ version: 1, categories: [{ label: "X", items: [] }] })).not.toThrow();
  });
});
