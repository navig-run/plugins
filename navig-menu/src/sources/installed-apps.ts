import type { DetectContext } from "../detectors/context.js";
import type { MenuSource, SourceFinding } from "./index.js";
import { emptyFinding } from "./index.js";

/**
 * v1.1 — detect installed desktop apps and expose launchers:
 *  - Windows: registry uninstall keys / Start Menu shortcuts / `winget list`
 *  - macOS: /Applications
 *  - Linux: XDG `.desktop` entries
 *
 * Stubbed now (interface only) so the full system menu drops in without touching the builder.
 */
export class InstalledAppsSource implements MenuSource {
  readonly id = "installed-apps";
  detect(_ctx: DetectContext): SourceFinding {
    return emptyFinding();
  }
}
