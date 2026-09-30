import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { mkdtempSync, rmSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { diagnose } from "../src/runners/diagnose.js";
import type { Action } from "../src/manifest/schema.js";

/** Minimal Action factory (only fields diagnose reads). */
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

describe("diagnose", () => {
  let root: string;
  beforeAll(() => {
    // A monorepo where the root `dev` chains down into a sub-app (`tauri-ui`) that
    // has a package.json but no node_modules — the real Keysni failure shape.
    root = mkdtempSync(join(tmpdir(), "navig-menu-dx-"));
    writeFileSync(join(root, "package.json"), JSON.stringify({ name: "keysni" }));
    mkdirSync(join(root, "tauri-ui"));
    writeFileSync(join(root, "tauri-ui", "package.json"), JSON.stringify({ name: "crxsentry-ui" }));
  });
  afterAll(() => rmSync(root, { recursive: true, force: true }));

  it("returns null on success or empty output", () => {
    expect(diagnose(act(), root, 0, "irrelevant")).toBeNull();
    expect(diagnose(act(), root, 1, "   ")).toBeNull();
  });

  it("Windows 'not recognized' → install fix targeting the cd'd sub-dir", () => {
    const captured = [
      "> keysni@0.1.0 tauri:dev",
      "> cd tauri-ui && npm run tauri dev",
      "> crxsentry-ui@0.1.0 tauri",
      "> tauri dev",
      "'tauri' is not recognized as an internal or external command,",
    ].join("\n");
    const dx = diagnose(act(), root, 1, captured);
    expect(dx?.title).toContain("tauri");
    expect(dx?.fix).toBeDefined();
    expect(dx?.fix?.launcher).toBe("npm");
    expect(dx?.fix?.argv).toEqual(["install"]);
    // The fix must run in tauri-ui/, NOT the repo root (that was the whole bug).
    expect(dx?.fix?.cwd).toBe(join(root, "tauri-ui"));
  });

  it("*nix 'command not found' is recognized too", () => {
    const dx = diagnose(act(), root, 127, "sh: 1: tauri: command not found");
    expect(dx?.title).toContain("tauri");
    expect(dx?.fix).toBeDefined();
  });

  it("module resolution failure → dependencies-not-installed fix", () => {
    const dx = diagnose(act(), root, 1, "Error: Cannot find module 'vite'");
    expect(dx?.title).toMatch(/Dependencies/i);
    expect(dx?.fix?.argv).toEqual(["install"]);
  });

  it("missing npm script → advice, no fix", () => {
    const dx = diagnose(act(), root, 1, "npm error Missing script: \"buildx\"");
    expect(dx?.title).toContain("buildx");
    expect(dx?.fix).toBeUndefined();
  });

  it("port in use → advice only", () => {
    const dx = diagnose(act(), root, 1, "Error: listen EADDRINUSE: address already in use :::1420");
    expect(dx?.title).toMatch(/in use/i);
    expect(dx?.fix).toBeUndefined();
  });

  it("python ModuleNotFoundError → advice only (no risky pip autofix)", () => {
    const dx = diagnose(act({ launcher: "python" }), root, 1, "ModuleNotFoundError: No module named 'requests'");
    expect(dx?.title).toContain("requests");
    expect(dx?.fix).toBeUndefined();
  });

  it("uses the lockfile's package manager for the fix when launcher isn't a PM", () => {
    writeFileSync(join(root, "pnpm-lock.yaml"), "");
    const dx = diagnose(act({ launcher: "node", argv: ["x.js"] }), root, 1, "Cannot find module 'left-pad'");
    expect(dx?.fix?.launcher).toBe("pnpm");
    rmSync(join(root, "pnpm-lock.yaml"));
  });

  it("unknown error → null (stays quiet, no noise)", () => {
    expect(diagnose(act(), root, 1, "Segmentation fault (core dumped)")).toBeNull();
  });
});
