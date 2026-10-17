import type { Theme } from "./theme.js";

/**
 * Shared formatting for the non-interactive command reports (doctor / import / organize / scan).
 * One vocabulary so every report reads as the same system, built on the theme primitives so it
 * degrades cleanly to ASCII under `--plain` / no-Unicode. Accessibility: status meaning is always
 * carried by a glyph + word, never colour alone. Each helper returns a ready-to-`console.log` string.
 */

const PAD = "  ";
const indent = (depth: number): string => PAD.repeat(1 + Math.max(0, depth));

/** Section header: an accent title over a thin rule. */
export function header(theme: Theme, title: string): string {
  const { c, accent, unicode } = theme;
  const bar = (unicode ? "─" : "-").repeat(Math.max(title.length, 10));
  return `${indent(0)}${accent(title)}\n${indent(0)}${c.dim(bar)}`;
}

export type StatusKind = "ok" | "warn" | "fail" | "info" | "muted" | "add" | "remove";

function statusGlyph(theme: Theme, kind: StatusKind): string {
  const { c, sym } = theme;
  switch (kind) {
    case "ok":
      return c.green(sym.complete);
    case "warn":
      return c.yellow(sym.warning);
    case "fail":
      return c.red(sym.failed);
    case "info":
      return theme.accent(sym.ready);
    case "muted":
      return c.dim(sym.unknown);
    case "add":
      return c.green("+");
    case "remove":
      return c.yellow("-");
  }
}

/** A status line: themed glyph + text. */
export function status(theme: Theme, kind: StatusKind, text: string, depth = 0): string {
  return `${indent(depth)}${statusGlyph(theme, kind)} ${text}`;
}

/** A padded key/value row for aligned audits. */
export function kv(theme: Theme, label: string, value: string, opts: { pad?: number; depth?: number } = {}): string {
  return `${indent(opts.depth ?? 0)}${theme.c.dim(label.padEnd(opts.pad ?? 12))} ${value}`;
}

/** A dim secondary / hint line. */
export function hint(theme: Theme, text: string, depth = 0): string {
  return `${indent(depth)}${theme.c.dim(text)}`;
}

/** A dim bullet item (`· text`). */
export function bullet(theme: Theme, text: string, depth = 0): string {
  return `${indent(depth)}${theme.c.dim(theme.sym.dot)} ${text}`;
}
