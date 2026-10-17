import { existsSync } from "node:fs";
import { join, isAbsolute, sep } from "node:path";
import { delimiter } from "node:path";

const isWin = process.platform === "win32";

/**
 * Resolve a launcher name to a concrete executable path on PATH, honoring PATHEXT on Windows
 * (so `pnpm` → `pnpm.cmd`). Returning the real path lets us spawn an argv ARRAY with no shell,
 * which avoids both the Windows `.cmd` ENOENT footgun and shell-interpolation injection.
 * Falls back to the bare name if nothing is found (execa can still resolve some cases).
 */
export function resolveLauncher(name: string): string {
  if (name.includes(sep) || name.includes("/") || isAbsolute(name)) return name;

  const paths = (process.env.PATH ?? "").split(delimiter).filter(Boolean);
  const exts = isWin
    ? (process.env.PATHEXT ?? ".COM;.EXE;.BAT;.CMD").split(";").map((e) => e.toLowerCase())
    : [""];

  for (const dir of paths) {
    for (const ext of exts) {
      const candidate = join(dir, name + ext);
      if (existsSync(candidate)) return candidate;
    }
    // Also accept an exact name already carrying its extension.
    const exact = join(dir, name);
    if (existsSync(exact)) return exact;
  }
  return name;
}
