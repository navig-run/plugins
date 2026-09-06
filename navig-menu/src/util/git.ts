import { execaSync } from "execa";

export interface GitInfo {
  branch?: string;
  sha?: string;
  dirty?: number;
  isRepo: boolean;
}

/** Best-effort git state for the banner. Never throws; absent git just yields isRepo=false. */
export function gitInfo(root: string): GitInfo {
  try {
    const branch = execaSync("git", ["rev-parse", "--abbrev-ref", "HEAD"], {
      cwd: root,
      reject: false,
      timeout: 1500,
    }).stdout?.trim();
    if (!branch) return { isRepo: false };
    const sha = execaSync("git", ["rev-parse", "--short", "HEAD"], {
      cwd: root,
      reject: false,
      timeout: 1500,
    }).stdout?.trim() || undefined;
    const status = execaSync("git", ["status", "--porcelain"], {
      cwd: root,
      reject: false,
      timeout: 1500,
    }).stdout;
    const dirty = status ? status.split("\n").filter((l) => l.trim().length).length : 0;
    return { branch, sha, dirty, isRepo: true };
  } catch {
    return { isRepo: false };
  }
}
