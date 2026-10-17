/**
 * Parse a project's GitHub remote from its `.git/config` — pure, so the plugin's `detect`/`contribute`
 * (which may not spawn) can recognize a GitHub repo without shelling out to git. Handles the three
 * URL forms git uses (https, ssh://, and scp-like `git@host:owner/repo`), strips a trailing `.git`,
 * and recognizes github.com plus GitHub Enterprise hosts. The origin remote wins; otherwise the first
 * GitHub-looking remote is used.
 */

export interface GithubRemote {
  owner: string;
  repo: string;
  host: string;
  /** The remote name it was found under (usually "origin"). */
  remote: string;
}

/** True for github.com and GitHub Enterprise hosts (anything with "github" in the hostname). */
export function isGithubHost(host: string): boolean {
  return /(^|\.)github\.com$/i.test(host) || /github/i.test(host);
}

/** Parse owner/repo/host out of a single git remote URL (any of git's URL forms). */
export function parseGithubRemote(url: string): Omit<GithubRemote, "remote"> | undefined {
  if (!url) return undefined;
  let host = "";
  let path = "";

  const scp = url.match(/^[^/@]+@([^:/]+):(.+)$/); // git@github.com:owner/repo.git
  if (scp) {
    host = scp[1]!;
    path = scp[2]!;
  } else {
    const m = url.match(/^[a-z][a-z0-9+.-]*:\/\/(?:[^@/]+@)?([^/]+)\/(.+)$/i); // https:// or ssh://
    if (!m) return undefined;
    host = m[1]!;
    path = m[2]!;
  }

  path = path.replace(/\.git$/i, "").replace(/^\/+|\/+$/g, "");
  const parts = path.split("/").filter(Boolean);
  if (parts.length < 2) return undefined;
  return { owner: parts[0]!, repo: parts[1]!, host };
}

/** Extract every `[remote "name"] url = …` from a `.git/config` (INI). */
export function parseGitConfigRemotes(text: string): Record<string, string> {
  const remotes: Record<string, string> = {};
  let current = "";
  for (const line of text.split(/\r?\n/)) {
    const section = line.match(/^\s*\[remote\s+"([^"]+)"\]/);
    if (section) {
      current = section[1]!;
      continue;
    }
    if (/^\s*\[/.test(line)) {
      current = ""; // entered a non-remote section
      continue;
    }
    const url = line.match(/^\s*url\s*=\s*(.+?)\s*$/);
    if (current && url) remotes[current] = url[1]!;
  }
  return remotes;
}

/** Resolve the project's GitHub remote from a `.git/config` body (origin preferred). */
export function githubRemoteFromConfig(text: string): GithubRemote | undefined {
  const remotes = parseGitConfigRemotes(text);
  const ordered: Array<[string, string]> = [
    ...(remotes.origin ? [["origin", remotes.origin] as [string, string]] : []),
    ...Object.entries(remotes).filter(([name]) => name !== "origin"),
  ];
  for (const [name, url] of ordered) {
    const parsed = parseGithubRemote(url);
    if (parsed && isGithubHost(parsed.host)) return { ...parsed, remote: name };
  }
  return undefined;
}
