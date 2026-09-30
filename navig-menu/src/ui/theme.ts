import chalk, { Chalk, type ChalkInstance } from "chalk";
import type { GlyphStyle } from "../manifest/schema.js";

export interface Theme {
  color: boolean;
  /** Box-drawing + geometric glyphs render (Windows legacy conhost may not). */
  unicode: boolean;
  /** Full emoji render (the cosmic banner icons). Implies unicode. */
  emoji: boolean;
  c: ChalkInstance;
  accent: (s: string) => string;
  /** Named accent (so the settings panel can show/cycle it). */
  accentName: string;
  sym: Record<SymKey, string>;
}

export type SymKey =
  | "ready"
  | "unknown"
  | "scanning"
  | "warning"
  | "failed"
  | "complete"
  | "pointer"
  | "dot"
  | "back";

const UNICODE: Record<SymKey, string> = {
  ready: "●",
  unknown: "◌",
  scanning: "◐",
  warning: "!",
  failed: "×",
  complete: "✓",
  pointer: "❯",
  dot: "·",
  back: "←",
};

const ASCII: Record<SymKey, string> = {
  ready: "*",
  unknown: "o",
  scanning: ".",
  warning: "!",
  failed: "x",
  complete: "v",
  pointer: ">",
  dot: "-",
  back: "<",
};

export interface ThemeOptions {
  plain?: boolean;
  accent?: string;
  glyphs?: GlyphStyle;
}

const ACCENTS: Record<string, (c: ChalkInstance) => (s: string) => string> = {
  cyan: (c) => c.cyanBright,
  blue: (c) => c.blueBright,
  green: (c) => c.greenBright,
  magenta: (c) => c.magentaBright,
  purple: (c) => c.magenta,
  yellow: (c) => c.yellowBright,
  red: (c) => c.redBright,
};

export const ACCENT_NAMES = Object.keys(ACCENTS);

export function createTheme(opts: ThemeOptions = {}): Theme {
  // chalk already honors NO_COLOR / non-TTY; --plain forces level 0 + ASCII.
  const plain = opts.plain === true;
  const color = !plain && chalk.level > 0;
  const c = plain ? new Chalk({ level: 0 }) : chalk;

  // Glyph fidelity: explicit `glyphs` setting wins; otherwise auto-detect.
  const autoUnicode = !plain && supportsUnicode();
  const glyphs: GlyphStyle = opts.glyphs ?? (plain ? "ascii" : autoUnicode ? "emoji" : "ascii");
  const unicode = glyphs !== "ascii";
  const emoji = glyphs === "emoji";

  const accentName = ACCENTS[opts.accent ?? ""] ? opts.accent! : "cyan";
  const accentFn = ACCENTS[accentName]!(c);
  return {
    color,
    unicode,
    emoji,
    c,
    accent: accentFn,
    accentName,
    sym: unicode ? UNICODE : ASCII,
  };
}

function supportsUnicode(): boolean {
  if (process.platform !== "win32") return true;
  // Windows Terminal / VS Code / modern shells set these; legacy conhost may not render glyphs.
  return Boolean(
    process.env.WT_SESSION ||
      process.env.TERM_PROGRAM === "vscode" ||
      process.env.ConEmuTask ||
      process.env.TERM === "xterm-256color",
  );
}
