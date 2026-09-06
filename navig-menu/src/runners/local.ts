import { execa } from "execa";
import { join, isAbsolute } from "node:path";
import type { Action } from "../manifest/schema.js";
import { resolveLauncher } from "../util/which.js";

export interface RunResult {
  exitCode: number;
  cancelled: boolean;
  /** Bounded tail of the child's stderr (for post-mortem diagnosis). "" if none. */
  stderrTail: string;
}

/** How much trailing stderr to keep for diagnosis — enough for a stack/lifecycle echo. */
const STDERR_CAPTURE_LIMIT = 64 * 1024;

/**
 * Execute an action locally. Always an argv ARRAY — no shell, no string interpolation. stdout and
 * stdin stay inherited so long-running dev servers keep a real TTY (colors, cursor control) and
 * interactive prompts work; Ctrl+C reaches the child through the shared console (we catch SIGINT to
 * return 130 without abruptly killing our own process first). stderr is TEE'd — streamed live AND
 * kept as a bounded tail so a failed run can be diagnosed (see `diagnose`). `buffer: false` stops
 * execa from separately accumulating the whole stream (memory-safe for chatty long runs).
 */
export async function runActionLocal(action: Action, root: string): Promise<RunResult> {
  const cwd = isAbsolute(action.cwd) ? action.cwd : join(root, action.cwd);
  const file = resolveLauncher(action.launcher);

  const subprocess = execa(file, action.argv, {
    cwd,
    stdio: ["inherit", "inherit", "pipe"],
    reject: false,
    cleanup: true,
    windowsHide: false,
    buffer: false,
  });

  let tail = "";
  subprocess.stderr?.on("data", (chunk: Buffer) => {
    process.stderr.write(chunk); // keep the error visible to the user, live
    tail += chunk.toString("utf8");
    if (tail.length > STDERR_CAPTURE_LIMIT) tail = tail.slice(tail.length - STDERR_CAPTURE_LIMIT);
  });

  let cancelled = false;
  const onSigint = () => {
    cancelled = true;
    // The child shares the console and receives Ctrl+C directly; we just note it and wait.
  };
  process.on("SIGINT", onSigint);

  try {
    const result = await subprocess;
    return { exitCode: cancelled ? 130 : (result.exitCode ?? 0), cancelled, stderrTail: tail };
  } catch {
    return { exitCode: cancelled ? 130 : 1, cancelled, stderrTail: tail };
  } finally {
    process.off("SIGINT", onSigint);
  }
}
