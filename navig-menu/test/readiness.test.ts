import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync, rmSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { checkReadiness } from "../src/detectors/readiness.js";
import type { Manifest } from "../src/manifest/schema.js";

/** Minimal manifest — checkReadiness only reads packageManager.value. */
function manifest(pm = "npm"): Manifest {
  return { packageManager: { value: pm, confidence: "detected", evidence: [] } } as unknown as Manifest;
}

describe("checkReadiness", () => {
  let root: string;
  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), "navig-menu-ready-"));
  });
  afterEach(() => {
    delete process.env.VIRTUAL_ENV;
    rmSync(root, { recursive: true, force: true });
  });

  it("clean node project with node_modules → ready (no issues)", () => {
    writeFileSync(join(root, "package.json"), "{}");
    mkdirSync(join(root, "node_modules"));
    expect(checkReadiness(root, manifest())).toEqual([]);
  });

  it("package.json without node_modules → deps issue with an install fix", () => {
    writeFileSync(join(root, "package.json"), "{}");
    const issues = checkReadiness(root, manifest("pnpm"));
    const deps = issues.find((i) => i.kind === "deps");
    expect(deps).toBeDefined();
    expect(deps?.fix).toEqual({ launcher: "pnpm", argv: ["install"], label: "pnpm install" });
  });

  it("falls back to npm when the package manager is unknown", () => {
    writeFileSync(join(root, "package.json"), "{}");
    const deps = checkReadiness(root, manifest("none")).find((i) => i.kind === "deps");
    expect(deps?.fix?.launcher).toBe("npm");
  });

  it(".env.example without .env → env issue, advice-only (no fix)", () => {
    writeFileSync(join(root, "package.json"), "{}");
    mkdirSync(join(root, "node_modules"));
    writeFileSync(join(root, ".env.example"), "API_KEY=");
    const env = checkReadiness(root, manifest()).find((i) => i.kind === "env");
    expect(env).toBeDefined();
    expect(env?.fix).toBeUndefined();
  });

  it("no env issue once a real .env exists", () => {
    writeFileSync(join(root, "package.json"), "{}");
    mkdirSync(join(root, "node_modules"));
    writeFileSync(join(root, ".env.example"), "API_KEY=");
    writeFileSync(join(root, ".env"), "API_KEY=x");
    expect(checkReadiness(root, manifest()).some((i) => i.kind === "env")).toBe(false);
  });

  it("python deps without a virtualenv → advice-only issue", () => {
    writeFileSync(join(root, "requirements.txt"), "requests\n");
    const py = checkReadiness(root, manifest("none")).find((i) => i.kind === "python-venv");
    expect(py).toBeDefined();
    expect(py?.fix).toBeUndefined();
  });

  it("no python-venv issue when a venv is active", () => {
    writeFileSync(join(root, "requirements.txt"), "requests\n");
    process.env.VIRTUAL_ENV = "/some/venv";
    expect(checkReadiness(root, manifest("none")).some((i) => i.kind === "python-venv")).toBe(false);
  });
});
