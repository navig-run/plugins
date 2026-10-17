import type { DetectContext } from "../detectors/context.js";
import type { MenuSource, SourceFinding } from "./index.js";
import { emptyFinding } from "./index.js";

/**
 * v1.1 — "relay any CLI menu". Detect installed CLI tools on PATH (reusing the same approach
 * as NAVIG's LocalDiscovery/shutil.which) and surface curated subcommand maps for common tools
 * (git, docker, gh, kubectl, wrangler, supabase, stripe, npm, pnpm, cargo, …).
 *
 * Stubbed now so the builder + UI are written against the final source interface; wiring the
 * PATH scan + subcommand catalog lands in v1.1.
 */
export class SystemCliSource implements MenuSource {
  readonly id = "system-cli";
  detect(_ctx: DetectContext): SourceFinding {
    return emptyFinding();
  }
}
