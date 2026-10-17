import type { DetectContext } from "../detectors/context.js";
import type { Action, Warning } from "../manifest/schema.js";

/**
 * A pluggable menu source. v1 ships ProjectScriptsSource fully; system-CLI and installed-apps
 * sources implement the same interface (stubbed for v1.1) so the builder treats every origin
 * — project scripts, installed CLIs, desktop apps, AI/custom — uniformly.
 */
export interface SourceFinding {
  actions: Action[];
  scripts?: Record<string, string>;
  warnings?: Warning[];
}

export interface MenuSource {
  readonly id: string;
  detect(ctx: DetectContext): SourceFinding;
}

export const emptyFinding = (): SourceFinding => ({ actions: [], scripts: {}, warnings: [] });
