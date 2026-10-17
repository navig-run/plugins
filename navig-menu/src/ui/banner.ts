import type { Theme } from "./theme.js";
import type { MenuModel } from "../builder/build.js";
import type { GitInfo } from "../util/git.js";
import { renderCosmicBanner } from "./cosmic.js";
import { resolveSettings } from "./settings.js";
import { TOOL_VERSION } from "../config/constants.js";

/**
 * Standalone cosmic banner as a string (e.g. for embedding outside the live TUI). The interactive
 * menu builds the banner directly via {@link renderCosmicBanner}; this wrapper just applies the
 * resolved settings + a sensible width so callers get the same look.
 */
export function renderBanner(model: MenuModel, theme: Theme, git: GitInfo, mode: string): string {
  const settings = resolveSettings(model.ui, model.accent);
  const width = Math.max(40, Math.min(86, (process.stdout.columns ?? 90) - 4));
  return renderCosmicBanner(model, { theme, git, mode, settings, toolVersion: TOOL_VERSION }, width).join("\n");
}
