/**
 * Minimal, string-aware JSONC → JSON normalizer. Strips `//` line comments, `/* *​/` block
 * comments, and trailing commas so `JSON.parse` accepts a `wrangler.jsonc`. It is deliberately NOT
 * a full JSON5 parser — wrangler configs only use standard JSON strings plus comments and the
 * occasional trailing comma, which is exactly what this covers. Never touches characters inside a
 * string literal (so a `//` or comma inside a value is preserved).
 */
export function stripJsonc(input: string): string {
  const out: string[] = [];
  let inStr = false;
  let i = 0;
  const n = input.length;

  // Drop trailing whitespace + a single trailing comma already emitted (called before a } or ]).
  const trimTrailingComma = (): void => {
    let j = out.length - 1;
    while (j >= 0 && /\s/.test(out[j]!)) j--;
    if (j >= 0 && out[j] === ",") out.splice(j, out.length - j);
  };

  while (i < n) {
    const ch = input[i]!;
    const next = i + 1 < n ? input[i + 1]! : "";

    if (inStr) {
      out.push(ch);
      if (ch === "\\" && i + 1 < n) {
        out.push(input[i + 1]!); // copy the escaped char verbatim
        i += 2;
        continue;
      }
      if (ch === '"') inStr = false;
      i++;
      continue;
    }

    if (ch === '"') {
      inStr = true;
      out.push(ch);
      i++;
      continue;
    }
    if (ch === "/" && next === "/") {
      i += 2;
      while (i < n && input[i] !== "\n") i++;
      continue;
    }
    if (ch === "/" && next === "*") {
      i += 2;
      while (i < n && !(input[i] === "*" && input[i + 1] === "/")) i++;
      i += 2; // skip the closing */
      continue;
    }
    if (ch === "}" || ch === "]") {
      trimTrailingComma();
      out.push(ch);
      i++;
      continue;
    }
    out.push(ch);
    i++;
  }
  return out.join("");
}
