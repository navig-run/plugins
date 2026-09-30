import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { aiAvailable, aiDiagnose, buildDiagnosisPrompt, aiEnrichMenu, aiExplainAction, aiPickAction, aiSuggestScript } from "../src/runners/ai.js";
import type { Action } from "../src/manifest/schema.js";

function act(over: Partial<Action> = {}): Action {
  return {
    id: "dev",
    label: "dev",
    group: "Dev",
    launcher: "npm",
    argv: ["run", "dev"],
    cwd: ".",
    risk: "safe",
    longRunning: false,
    confidence: "detected",
    evidence: [],
    source: "project-scripts",
    ...over,
  } as Action;
}

/** A canned Anthropic /v1/messages response wrapping assistant text. */
function anthropicReply(text: string) {
  return { ok: true, json: async () => ({ content: [{ type: "text", text }] }) };
}

const KEYS = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "NAVIG_MENU_AI_KEY", "NAVIG_MENU_AI_PROVIDER", "NAVIG_MENU_AI_MODEL"];

describe("ai", () => {
  let saved: Record<string, string | undefined>;
  beforeEach(() => {
    saved = {};
    for (const k of KEYS) {
      saved[k] = process.env[k];
      delete process.env[k];
    }
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    for (const k of KEYS) {
      if (saved[k] === undefined) delete process.env[k];
      else process.env[k] = saved[k];
    }
  });

  it("aiAvailable reflects presence of a key", () => {
    expect(aiAvailable()).toBe(false);
    process.env.ANTHROPIC_API_KEY = "sk-test";
    expect(aiAvailable()).toBe(true);
  });

  it("aiDiagnose returns null with no key (no network call)", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const out = await aiDiagnose(act(), "/proj", 1, "some error");
    expect(out).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("parses an Anthropic JSON diagnosis (even with markdown fences)", async () => {
    process.env.ANTHROPIC_API_KEY = "sk-test";
    const body = '```json\n{"title":"Build failed","hint":"Run the codegen step first.","command":"npm run codegen"}\n```';
    vi.stubGlobal("fetch", vi.fn(async () => anthropicReply(body)));
    const out = await aiDiagnose(act(), "/proj", 1, "unknown error");
    expect(out?.title).toBe("Build failed");
    expect(out?.hint).toMatch(/codegen/);
    expect(out?.command).toBe("npm run codegen");
  });

  it("strips a destructive suggested command", async () => {
    process.env.ANTHROPIC_API_KEY = "sk-test";
    const body = '{"title":"Reset repo","hint":"Wipe and retry.","command":"git reset --hard HEAD"}';
    vi.stubGlobal("fetch", vi.fn(async () => anthropicReply(body)));
    const out = await aiDiagnose(act(), "/proj", 1, "err");
    expect(out?.hint).toMatch(/Wipe/);
    expect(out?.command).toBeUndefined(); // destructive → never surfaced
  });

  it("returns null on a non-OK HTTP response", async () => {
    process.env.ANTHROPIC_API_KEY = "sk-test";
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, json: async () => ({}) })));
    expect(await aiDiagnose(act(), "/proj", 1, "err")).toBeNull();
  });

  it("returns null when the model emits no JSON", async () => {
    process.env.ANTHROPIC_API_KEY = "sk-test";
    vi.stubGlobal("fetch", vi.fn(async () => anthropicReply("I am not sure what went wrong.")));
    expect(await aiDiagnose(act(), "/proj", 1, "err")).toBeNull();
  });

  it("buildDiagnosisPrompt embeds the command, cwd and stderr", () => {
    const p = buildDiagnosisPrompt(act(), "/proj", 127, "tauri: command not found");
    expect(p).toMatch(/npm run dev/);
    expect(p).toMatch(/tauri: command not found/);
    expect(p).toMatch(/Exit code: 127/);
  });

  const enrichInput = {
    name: "myapp",
    stack: ["vite", "react"],
    scripts: [
      { id: "dev", cmd: "vite" },
      { id: "build", cmd: "vite build" },
    ],
  };

  it("aiEnrichMenu returns null with no key and makes no call", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    expect(await aiEnrichMenu(enrichInput)).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("aiEnrichMenu returns null for an empty script list (no call)", async () => {
    process.env.ANTHROPIC_API_KEY = "sk-test";
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    expect(await aiEnrichMenu({ ...enrichInput, scripts: [] })).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("aiEnrichMenu parses purpose + overrides and drops unknown ids", async () => {
    process.env.ANTHROPIC_API_KEY = "sk-test";
    const body = JSON.stringify({
      purpose: "A Vite + React app.",
      overrides: {
        dev: { label: "Vite dev", description: "Start the Vite dev server.", group: "Dev" },
        build: { label: "Build", description: "Production build.", group: "Build" },
        ghost: { label: "Nope", description: "Not a real script.", group: "X" }, // unknown id → dropped
      },
    });
    vi.stubGlobal("fetch", vi.fn(async () => anthropicReply(body)));
    const out = await aiEnrichMenu(enrichInput);
    expect(out?.purpose).toBe("A Vite + React app.");
    expect(Object.keys(out?.overrides ?? {})).toEqual(["dev", "build"]); // ghost filtered out
    expect(out?.overrides?.dev?.label).toBe("Vite dev");
  });

  it("aiEnrichMenu never emits a risk field (safety stays heuristic + human)", async () => {
    process.env.ANTHROPIC_API_KEY = "sk-test";
    const body = JSON.stringify({
      overrides: { dev: { label: "Dev", description: "x", group: "Dev", risk: "safe" } },
    });
    vi.stubGlobal("fetch", vi.fn(async () => anthropicReply(body)));
    const out = await aiEnrichMenu(enrichInput);
    expect(out?.overrides?.dev).not.toHaveProperty("risk");
  });

  it("aiSuggestScript refuses destructive canonicals without calling the model", async () => {
    process.env.ANTHROPIC_API_KEY = "sk-test";
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    for (const canonical of ["reset", "migrate", "seed", "deploy", "clean"]) {
      expect(await aiSuggestScript(canonical, ["node"], ["build: tsc"])).toBeNull();
    }
    expect(fetchMock).not.toHaveBeenCalled(); // guarded before any provider call
  });

  it("aiSuggestScript still suggests a body for a safe canonical", async () => {
    process.env.ANTHROPIC_API_KEY = "sk-test";
    vi.stubGlobal("fetch", vi.fn(async () => anthropicReply("tsc --noEmit")));
    expect(await aiSuggestScript("typecheck", ["typescript"], ["build: tsc"])).toBe("tsc --noEmit");
  });

  it("aiExplainAction returns null with no key and makes no call", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    expect(await aiExplainAction(act({ risk: "dangerous" }), "/proj")).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("aiExplainAction returns a plain-English summary", async () => {
    process.env.ANTHROPIC_API_KEY = "sk-test";
    const prose = "This drops every table and recreates the schema. It is not reversible; you lose all local data.";
    vi.stubGlobal("fetch", vi.fn(async () => anthropicReply(prose)));
    const out = await aiExplainAction(act({ launcher: "npm", argv: ["run", "db:reset"], risk: "dangerous" }), "/proj");
    expect(out).toMatch(/not reversible/i);
  });

  const candidates = [
    { id: "dev", label: "Dev", cmd: "vite" },
    { id: "build", label: "Build", cmd: "vite build" },
    { id: "db:reset", label: "Reset DB", cmd: "prisma migrate reset" },
  ];

  it("aiPickAction returns null with no key (no call)", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    expect(await aiPickAction("start the frontend", candidates)).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("aiPickAction maps a request to an existing id", async () => {
    process.env.ANTHROPIC_API_KEY = "sk-test";
    vi.stubGlobal("fetch", vi.fn(async () => anthropicReply("dev")));
    expect(await aiPickAction("start the frontend", candidates)).toBe("dev");
  });

  it("aiPickAction returns null on NONE", async () => {
    process.env.ANTHROPIC_API_KEY = "sk-test";
    vi.stubGlobal("fetch", vi.fn(async () => anthropicReply("NONE")));
    expect(await aiPickAction("order a pizza", candidates)).toBeNull();
  });

  it("aiPickAction rejects a hallucinated id not in the list", async () => {
    process.env.ANTHROPIC_API_KEY = "sk-test";
    vi.stubGlobal("fetch", vi.fn(async () => anthropicReply("deploy")));
    expect(await aiPickAction("ship it", candidates)).toBeNull();
  });

  it("aiPickAction tolerates punctuation/quoting around the id", async () => {
    process.env.ANTHROPIC_API_KEY = "sk-test";
    vi.stubGlobal("fetch", vi.fn(async () => anthropicReply("`db:reset`.")));
    expect(await aiPickAction("wipe the database", candidates)).toBe("db:reset");
  });
});
