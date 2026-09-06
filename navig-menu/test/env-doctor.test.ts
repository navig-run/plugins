import { describe, it, expect } from "vitest";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { parseEnvKeys, buildEnvAppend } from "../src/plugins/builtin/env-doctor/engine/dotenv.js";
import { exampleTarget, findEnvPairs, auditEnv } from "../src/plugins/builtin/env-doctor/config.js";
import { envDoctorBannerLines } from "../src/plugins/builtin/env-doctor/banner.js";
import { envDoctor } from "../src/plugins/builtin/env-doctor/index.js";
import type { PluginContext } from "../src/plugins/types.js";

/* ── parseEnvKeys — names only, never values ───────────────────────────────────────── */

describe("parseEnvKeys", () => {
  it("extracts names, honors `export`, ignores comments/blanks, dedups", () => {
    const text = `# a comment
API_KEY=secret
export TOKEN=abc123

DATABASE_URL=postgres://u:p@h/db
API_KEY=dupe
`;
    expect(parseEnvKeys(text)).toEqual(["API_KEY", "TOKEN", "DATABASE_URL"]);
  });

  it("NEVER returns a value — even when the value contains '=' or a URL", () => {
    const keys = parseEnvKeys(`SECRET=a=b=c==\nURL=https://x/y?z=1&k=2\n`);
    expect(keys).toEqual(["SECRET", "URL"]); // names only
    // no fragment of either value leaks into the returned names
    expect(keys.join("|")).not.toMatch(/a=b|c==|https|z=1/);
  });
});

describe("buildEnvAppend", () => {
  it("appends empty KEY= lines (no values), fixing the trailing newline", () => {
    expect(buildEnvAppend("A=1", ["B", "C"])).toBe("A=1\nB=\nC=\n");
    expect(buildEnvAppend("A=1\n", ["B"])).toBe("A=1\nB=\n");
    expect(buildEnvAppend("", ["B"])).toBe("B=\n");
    expect(buildEnvAppend("A=1", [])).toBe("A=1");
  });
});

/* ── pair discovery ────────────────────────────────────────────────────────────────── */

describe("exampleTarget", () => {
  it("strips the example/sample/template suffix", () => {
    expect(exampleTarget(".env.example")).toBe(".env");
    expect(exampleTarget(".env.local.example")).toBe(".env.local");
    expect(exampleTarget(".dev.vars.example")).toBe(".dev.vars");
    expect(exampleTarget(".env.sample")).toBe(".env");
    expect(exampleTarget(".env.template")).toBe(".env");
  });
});

describe("findEnvPairs + auditEnv", () => {
  it("discovers pairs and audits missing/extra by name", () => {
    const dir = mkdtempSync(join(tmpdir(), "envd-"));
    try {
      writeFileSync(join(dir, ".env.example"), "API_KEY=\nTOKEN=\nDATABASE_URL=\n");
      writeFileSync(join(dir, ".env"), "API_KEY=real-value\nEXTRA=1\n");
      writeFileSync(join(dir, ".dev.vars.example"), "CF_TOKEN=\n");
      writeFileSync(join(dir, "notes.txt.example"), "not an env file");

      const pairs = findEnvPairs(dir);
      expect(pairs).toEqual([
        { example: ".dev.vars.example", target: ".dev.vars" },
        { example: ".env.example", target: ".env" },
      ]);

      const audit = auditEnv(dir, { example: ".env.example", target: ".env" });
      expect(audit.missing).toEqual(["TOKEN", "DATABASE_URL"]);
      expect(audit.extra).toEqual(["EXTRA"]);
      expect(audit.targetExists).toBe(true);

      const missingTarget = auditEnv(dir, { example: ".dev.vars.example", target: ".dev.vars" });
      expect(missingTarget.missing).toEqual(["CF_TOKEN"]);
      expect(missingTarget.targetExists).toBe(false);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

/* ── banner + detect/contribute ────────────────────────────────────────────────────── */

function makeCtx(root: string): PluginContext {
  return {
    root,
    manifest: { scripts: {} } as never,
    scripts: {},
    settings: {},
    cacheDir: join(root, ".cache"),
    hasFile: () => false,
    hasDep: () => false,
    readCache: () => undefined,
  } as unknown as PluginContext;
}

describe("envDoctorBannerLines", () => {
  it("counts missing keys (yellow) / all-set (green)", () => {
    const dir = mkdtempSync(join(tmpdir(), "envd-b-"));
    try {
      writeFileSync(join(dir, ".env.example"), "A=\nB=\nC=\n");
      writeFileSync(join(dir, ".env"), "A=1\n");
      expect(envDoctorBannerLines(makeCtx(dir))[0]).toEqual({ text: "env · 2 keys missing", tone: "yellow" });

      writeFileSync(join(dir, ".env"), "A=1\nB=2\nC=3\n");
      expect(envDoctorBannerLines(makeCtx(dir))[0]).toEqual({ text: "env · all keys set", tone: "green" });
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("returns [] with no example file", () => {
    const dir = mkdtempSync(join(tmpdir(), "envd-none-"));
    try {
      expect(envDoctorBannerLines(makeCtx(dir))).toEqual([]);
      expect(envDoctor.detect?.(makeCtx(dir))).toBe(false);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe("envDoctor.detect + contribute", () => {
  it("activates when an example env file exists and contributes the rail", () => {
    const dir = mkdtempSync(join(tmpdir(), "envd-p-"));
    try {
      writeFileSync(join(dir, ".env.example"), "A=\n");
      expect(envDoctor.detect?.(makeCtx(dir))).toBe(true);
      const ids = (envDoctor.contribute(makeCtx(dir)).sections?.[0]?.actions ?? []).map((a) => a.id);
      expect(ids).toEqual(["env.check", "env.scaffold"]);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
