/**
 * Locate the project's GitHub repo by reading its `.git/config` (pure, read-only). Used by both the
 * plugin's `detect` (is this a GitHub project?) and its banner/handlers (which owner/repo). Returns
 * undefined for a non-git dir, a git dir with no GitHub remote, or a linked worktree/submodule whose
 * `.git` is a file rather than a directory (the common `.git/config` case is what we support).
 */

import { join } from "node:path";
import { readText } from "../../../detectors/fs.js";
import { githubRemoteFromConfig, type GithubRemote } from "./engine/remote.js";

export type { GithubRemote } from "./engine/remote.js";

export function loadGithubRepo(root: string): GithubRemote | undefined {
  const text = readText(join(root, ".git", "config"));
  if (!text) return undefined;
  return githubRemoteFromConfig(text);
}
