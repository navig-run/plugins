import type { Action } from "../manifest/schema.js";

/**
 * Optional AI assist for the menu. The engine stays LLM-agnostic and offline-first: it bakes in
 * NO SDK and NO keys. When a standard provider key is present in the environment we make a single
 * native-`fetch` call (no dependency); otherwise callers fall back to `buildDiagnosisPrompt` — a
 * paste-ready block for any external AI. AI output is always ADVICE: suggested commands are shown,
 * never auto-run (that would violate the engine's "no shell strings, never invent commands" rule).
 *
 * Cost-aware: this is only ever invoked on explicit opt-in (`--ai` or an interactive keypress),
 * never automatically.
 */

export interface AiDiagnosis {
  title: string;
  hint: string;
  /** A single suggested shell command to fix it, or undefined. Shown only — never executed. */
  command?: string;
}

/** Commands we refuse to surface as a one-liner even as advice (defense in depth). */
const DESTRUCTIVE =
  /\brm\s+-rf|\bgit\s+(reset\s+--hard|clean\s+-\w*f|push\s+.*--force)|\b(mkfs|dd\s+if=|format)\b|DROP\s+(TABLE|DATABASE)|>\s*\/dev\/sd/i;

interface Provider {
  id: "anthropic" | "openai";
  key: string;
  model: string;
}

function resolveProvider(): Provider | null {
  const model = process.env.NAVIG_MENU_AI_MODEL;
  // 1) Explicit navig-injected credential (see the `navig menu` launcher).
  const injected = process.env.NAVIG_MENU_AI_KEY;
  if (injected) {
    const id = process.env.NAVIG_MENU_AI_PROVIDER === "openai" ? "openai" : "anthropic";
    return { id, key: injected, model: model ?? defaultModel(id) };
  }
  // 2) Standard provider keys.
  if (process.env.ANTHROPIC_API_KEY) {
    return { id: "anthropic", key: process.env.ANTHROPIC_API_KEY, model: model ?? defaultModel("anthropic") };
  }
  if (process.env.OPENAI_API_KEY) {
    return { id: "openai", key: process.env.OPENAI_API_KEY, model: model ?? defaultModel("openai") };
  }
  return null;
}

function defaultModel(id: "anthropic" | "openai"): string {
  return id === "anthropic" ? "claude-haiku-4-5-20251001" : "gpt-4o-mini";
}

/** True when an inline AI call is possible (a key is available). */
export function aiAvailable(): boolean {
  return resolveProvider() !== null;
}

const SYSTEM = [
  "You are a senior developer diagnosing why a shell command failed in a project.",
  "Reply with ONLY minified JSON, no prose, no markdown fences:",
  '{"title":"<=6 words","hint":"1-2 sentences, concrete next step","command":"<single safe shell command to fix, or empty string>"}',
  "Never suggest destructive commands (rm -rf, git reset --hard, force push, DROP, format, dd).",
  "If unsure, set command to an empty string and explain in hint.",
].join(" ");

function userPrompt(action: Action, root: string, exitCode: number, stderr: string): string {
  const cmd = `${action.launcher} ${action.argv.join(" ")}`.trim();
  const cwd = action.cwd === "." ? root : action.cwd;
  const tail = stderr.length > 4000 ? stderr.slice(stderr.length - 4000) : stderr;
  return [
    `Command: ${cmd}`,
    `Working dir: ${cwd}`,
    `Exit code: ${exitCode}`,
    `Stderr (tail):`,
    "```",
    tail.trim(),
    "```",
  ].join("\n");
}

/** The keyless fallback: a self-contained prompt the user can paste into any AI. */
export function buildDiagnosisPrompt(action: Action, root: string, exitCode: number, stderr: string): string {
  return `${SYSTEM}\n\n${userPrompt(action, root, exitCode, stderr)}`;
}

export function extractJson(text: string): unknown | null {
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start === -1 || end <= start) return null;
  try {
    return JSON.parse(text.slice(start, end + 1));
  } catch {
    return null;
  }
}

/**
 * Generic one-shot completion for features that need arbitrary AI text (e.g. the store plugin's
 * listing generator). Returns the model's raw text, or null when no provider key is available or
 * the call fails. Same offline-first contract as the rest of this module: no SDK, no bundled key.
 */
export async function aiComplete(system: string, user: string, maxTokens = 1024): Promise<string | null> {
  const p = resolveProvider();
  if (!p) return null;
  return callProvider(p, system, user, maxTokens);
}

async function callProvider(
  p: Provider,
  system: string,
  user: string,
  maxTokens = 512,
): Promise<string | null> {
  const signal = AbortSignal.timeout(25_000);
  try {
    if (p.id === "anthropic") {
      const res = await fetch("https://api.anthropic.com/v1/messages", {
        method: "POST",
        signal,
        headers: {
          "x-api-key": p.key,
          "anthropic-version": "2023-06-01",
          "content-type": "application/json",
        },
        body: JSON.stringify({
          model: p.model,
          max_tokens: maxTokens,
          system,
          messages: [{ role: "user", content: user }],
        }),
      });
      if (!res.ok) return null;
      const json: any = await res.json();
      const block = Array.isArray(json?.content) ? json.content.find((b: any) => b?.type === "text") : null;
      return typeof block?.text === "string" ? block.text : null;
    }
    // openai
    const res = await fetch("https://api.openai.com/v1/chat/completions", {
      method: "POST",
      signal,
      headers: { authorization: `Bearer ${p.key}`, "content-type": "application/json" },
      body: JSON.stringify({
        model: p.model,
        max_tokens: maxTokens,
        messages: [
          { role: "system", content: system },
          { role: "user", content: user },
        ],
      }),
    });
    if (!res.ok) return null;
    const json: any = await res.json();
    const content = json?.choices?.[0]?.message?.content;
    return typeof content === "string" ? content : null;
  } catch {
    return null; // network/timeout/parse — degrade silently, caller shows a fallback
  }
}

/**
 * Ask an LLM to diagnose a failed action. Returns null when no key is available or the call fails
 * (callers should then offer `buildDiagnosisPrompt`). Advice-only — the returned `command` is for
 * display, never executed here.
 */
export async function aiDiagnose(
  action: Action,
  root: string,
  exitCode: number,
  stderr: string,
): Promise<AiDiagnosis | null> {
  const p = resolveProvider();
  if (!p) return null;
  const text = await callProvider(p, SYSTEM, userPrompt(action, root, exitCode, stderr));
  if (!text) return null;
  const obj = extractJson(text) as { title?: unknown; hint?: unknown; command?: unknown } | null;
  if (!obj || typeof obj.hint !== "string" || !obj.hint.trim()) return null;
  const title = typeof obj.title === "string" && obj.title.trim() ? obj.title.trim() : "AI diagnosis";
  let command = typeof obj.command === "string" && obj.command.trim() ? obj.command.trim() : undefined;
  if (command && DESTRUCTIVE.test(command)) command = undefined; // never surface destructive one-liners
  return { title, hint: obj.hint.trim(), command };
}

// ── AI menu enrichment (`--ai build`) ─────────────────────────────────────────

/** DISPLAY-only annotations AI may propose for a detected script. No `risk` — safety stays
 *  heuristic + human, so AI can never lower a confirm/dangerous gate. */
export interface MenuOverride {
  label?: string;
  description?: string;
  group?: string;
}

export interface MenuEnrichment {
  purpose?: string;
  /** Keyed by the detected script id. */
  overrides?: Record<string, MenuOverride>;
}

export interface EnrichInput {
  name: string;
  stack: string[];
  /** Detected scripts to curate: id + the resolved command string. */
  scripts: { id: string; cmd: string; group?: string }[];
}

const ENRICH_SYSTEM = [
  "You curate a developer project's command menu.",
  "Given the stack and its scripts (id + command), reply with ONLY minified JSON, no prose, no fences:",
  '{"purpose":"one short line describing the project","overrides":{"<scriptId>":{"label":"<=3 words","description":"one concise line: what running it does","group":"<section>"}}}',
  "Rules: only use the given script ids — never invent commands or ids; write a description for EVERY script id you were given;",
  "labels short and stack-specific (e.g. 'Vite dev', 'Prisma migrate'); descriptions are a single concrete line;",
  "group each into one of: Dev, Build, Test, Quality, Database, Deploy, Ops, Utilities.",
  "JSON only.",
].join(" ");

/** Curate at most this many scripts per request — keeps each JSON reply within the token budget. */
const ENRICH_BATCH = 24;

function clampStr(v: unknown, max: number): string | undefined {
  if (typeof v !== "string") return undefined;
  const t = v.trim();
  return t ? t.slice(0, max) : undefined;
}

/**
 * Ask an LLM to curate a project's menu — a description for every script + better labels/grouping,
 * plus a one-line project purpose. Batched so a large monorepo (dozens of scripts) still fits the
 * per-reply token budget; results are aggregated. Returns display-only annotations the caller merges
 * so human edits always win. Null when no key or every batch fails. Never touches what runs.
 */
export async function aiEnrichMenu(input: EnrichInput): Promise<MenuEnrichment | null> {
  const p = resolveProvider();
  if (!p || input.scripts.length === 0) return null;

  const overrides: Record<string, MenuOverride> = {};
  let purpose: string | undefined;
  let anyOk = false;

  for (let start = 0; start < input.scripts.length; start += ENRICH_BATCH) {
    const batch = input.scripts.slice(start, start + ENRICH_BATCH);
    const wantPurpose = start === 0;
    const user = [
      `Project: ${input.name}`,
      `Stack: ${input.stack.join(", ") || "unknown"}`,
      wantPurpose ? "Include a one-line project purpose." : "Do not include a purpose.",
      "Scripts:",
      ...batch.map((s) => `- ${s.id}: ${s.cmd}${s.group ? ` [${s.group}]` : ""}`),
    ].join("\n");

    // Budget: ~40 tokens/script × 24 ≈ 1000, plus headroom for the JSON envelope.
    const text = await callProvider(p, ENRICH_SYSTEM, user, 1500);
    if (!text) continue;
    const obj = extractJson(text) as { purpose?: unknown; overrides?: unknown } | null;
    if (!obj) continue;
    anyOk = true;
    if (wantPurpose && !purpose) purpose = clampStr(obj.purpose, 120);

    const known = new Set(batch.map((s) => s.id));
    const raw = obj.overrides && typeof obj.overrides === "object" ? (obj.overrides as Record<string, any>) : {};
    for (const [id, entry] of Object.entries(raw)) {
      if (!known.has(id) || !entry || typeof entry !== "object") continue;
      const o: MenuOverride = {};
      const label = clampStr(entry.label, 40);
      const description = clampStr(entry.description, 160);
      const group = clampStr(entry.group, 32);
      if (label) o.label = label;
      if (description) o.description = description;
      if (group) o.group = group;
      if (Object.keys(o).length) overrides[id] = o;
    }
  }

  if (!anyOk) return null;
  if (!purpose && Object.keys(overrides).length === 0) return null;
  return { purpose, overrides: Object.keys(overrides).length ? overrides : undefined };
}

// ── AI script suggestion (doctor --fix: a body for a missing canonical) ────────

/**
 * Canonicals whose bodies an LLM must NEVER auto-author — they change state or are irreversible
 * (publish/schema/data/filesystem). A hallucinated `migrate reset --force` or `supabase db reset`
 * slips past any keyword denylist, so we refuse these at the source: they must be written by hand.
 */
export const AI_UNSAFE_CANONICALS = new Set(["deploy", "migrate", "seed", "clean", "reset"]);

const SCRIPT_SYSTEM = [
  "You suggest a package.json script body for a missing canonical action in a project.",
  "Given the canonical action, the stack, and a few existing scripts, reply with ONLY the raw command —",
  "no JSON, no quotes, no prose, no markdown. A single safe command using tools the stack already implies",
  "(e.g. 'tsc --noEmit' for typecheck, 'eslint .' for lint). Never suggest a destructive command.",
  "If you can't suggest a safe one, reply with the single word NONE.",
].join(" ");

/**
 * Suggest a script body for a missing canonical action (e.g. `typecheck` → `tsc --noEmit`). Advice
 * only — the caller shows it and requires confirmation before writing package.json. Returns null
 * when no key, no confident suggestion, or the model proposes something destructive.
 */
export async function aiSuggestScript(
  canonical: string,
  stack: string[],
  sampleScripts: string[],
): Promise<string | null> {
  const p = resolveProvider();
  if (!p) return null;
  // Never synthesize a body for an inherently destructive canonical — a keyword denylist can't be
  // trusted to catch every DB-wipe / force-deploy the model might invent.
  if (AI_UNSAFE_CANONICALS.has(canonical)) return null;
  const user = [
    `Canonical action: ${canonical}`,
    `Stack: ${stack.join(", ") || "unknown"}`,
    "Existing scripts (name: command):",
    ...sampleScripts,
  ].join("\n");
  const text = await callProvider(p, SCRIPT_SYSTEM, user, 120);
  if (!text) return null;
  const cmd = (text.trim().split("\n")[0] ?? "").replace(/^[`'"]+|[`'"]+$/g, "").trim();
  if (!cmd || cmd.toUpperCase() === "NONE") return null;
  if (DESTRUCTIVE.test(cmd)) return null;
  return cmd;
}

// ── AI pre-run safety review (dangerous actions) ──────────────────────────────

const EXPLAIN_SYSTEM = [
  "You explain what a shell command does BEFORE a developer runs it, so they can decide safely.",
  "Given the command and its working directory, reply with 2-4 plain sentences covering: what it does,",
  "what it changes (files / remote / database / services), whether it is reversible, and the main risk.",
  "No preamble, no markdown, no code fences, no bullet points — just the sentences.",
].join(" ");

/**
 * Plain-English risk summary for an action about to run. Advice-only (prose, no command). Returns
 * null when no key or the call fails. The caller should present it as an AI summary to verify, not
 * as ground truth.
 */
export async function aiExplainAction(action: Action, root: string): Promise<string | null> {
  const p = resolveProvider();
  if (!p) return null;
  const cmd = `${action.launcher} ${action.argv.join(" ")}`.trim();
  const cwd = action.cwd === "." ? root : action.cwd;
  const user = `Command: ${cmd}\nWorking dir: ${cwd}`;
  const text = await callProvider(p, EXPLAIN_SYSTEM, user);
  if (!text) return null;
  const trimmed = text.trim();
  return trimmed ? trimmed.slice(0, 600) : null;
}

// ── AI natural-language action mapping ────────────────────────────────────────

/** A menu action offered to the mapper. */
export interface PickCandidate {
  id: string;
  label: string;
  cmd: string;
}

const PICK_SYSTEM = [
  "You map a developer's natural-language request to exactly ONE existing menu action.",
  "Reply with ONLY the exact `id` of the best-matching action, or the single word NONE if nothing fits.",
  "Never invent an id and never output a command — only pick from the ids given.",
].join(" ");

/**
 * Resolve a free-text request (e.g. "start the frontend") to an existing action id. Map-only: it
 * can only return one of the given ids, never a synthesized command — so it can't run something the
 * project didn't already define. Returns null when no key, no match, or the model hallucinates.
 */
export async function aiPickAction(query: string, actions: PickCandidate[]): Promise<string | null> {
  const p = resolveProvider();
  if (!p || actions.length === 0 || !query.trim()) return null;
  const user =
    `Request: ${query}\n\nActions:\n` +
    actions.map((a) => `- ${a.id}: ${a.label} (${a.cmd})`).join("\n");
  const text = await callProvider(p, PICK_SYSTEM, user);
  if (!text) return null;
  const answer = text.trim().replace(/[`"'.]/g, "").split(/\s+/)[0] ?? "";
  if (!answer || answer.toUpperCase() === "NONE") return null;
  // Guard against a hallucinated id — only accept one that actually exists.
  return actions.some((a) => a.id === answer) ? answer : null;
}
