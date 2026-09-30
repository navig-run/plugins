/**
 * Cross-compile self-contained binaries with Bun from a single CI host. Each target embeds the
 * Bun runtime, so end users (and `navig menu`) need no Node installed. Run: `bun run scripts/build-binaries.ts`.
 */
import { mkdirSync } from "node:fs";
import { execFileSync } from "node:child_process";

const TARGETS = [
  "bun-linux-x64",
  "bun-linux-arm64",
  "bun-darwin-x64",
  "bun-darwin-arm64",
  "bun-windows-x64",
] as const;

mkdirSync("out", { recursive: true });

for (const target of TARGETS) {
  const ext = target.includes("windows") ? ".exe" : "";
  const outfile = `out/menu-${target.replace("bun-", "")}${ext}`;
  process.stdout.write(`\n▶ ${target} → ${outfile}\n`);
  execFileSync(
    "bun",
    ["build", "./src/cli.ts", "--compile", `--target=${target}`, `--outfile=${outfile}`],
    { stdio: "inherit" },
  );
}

process.stdout.write("\n✓ binaries in ./out — generate SHA256SUMS in CI before release.\n");
