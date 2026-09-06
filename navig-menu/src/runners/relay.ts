import type { Action } from "../manifest/schema.js";

/**
 * The relay contract: in relay mode the binary does NOT execute; it prints a single JSON
 * action-request on the last stdout line and exits. NAVIG parses this and runs it locally /
 * over SSH / via the gateway. `env` carries variable NAMES only — never values.
 */
export interface ActionRequest {
  type: "navig-menu/action-request";
  v: 1;
  action: string;
  launcher: string;
  argv: string[];
  cwd: string;
  risk: Action["risk"];
  longRunning: boolean;
}

export function toActionRequest(action: Action, root: string): ActionRequest {
  return {
    type: "navig-menu/action-request",
    v: 1,
    action: action.canonical ?? action.id,
    launcher: action.launcher,
    argv: action.argv,
    cwd: action.cwd === "." ? root : action.cwd,
    risk: action.risk,
    longRunning: action.longRunning,
  };
}

export function emitActionRequest(action: Action, root: string): void {
  process.stdout.write(JSON.stringify(toActionRequest(action, root)) + "\n");
}
