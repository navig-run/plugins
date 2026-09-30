// Pure triage helpers for the mailroom edge Worker. No I/O, no bindings — testable with
// `node --test`. Everything the Worker decides about a message is decided here.

/** Keyword tags, mirrored from the space's mailroom/rules.yaml (`echeances`, `facture`, `securite`). */
export const KEYWORD_TAGS = [
  ["echeance", /\b(?:[ée]ch[ée]ance|renouvellement|expiration|date limite|rappel|relance|mise en demeure)\b/i],
  ["facture", /\b(?:facture|invoice|paiement|payment|impay[ée]|reglement|r[èe]glement)\b/i],
  ["securite", /\b(?:vulnerabilit|security|s[ée]curit[ée]|disclosure|bug bounty|pentest)\b/i],
  ["urgent", /\b(?:urgent|asap|imm[ée]diat)\b/i],
];

export const SNIPPET_CHARS = 120;

/** `"Name <a@b.c>"` → `a@b.c` (lowercase). Envelope addresses arrive bare already. */
export function address(value) {
  const s = String(value || "").trim();
  const m = s.match(/<([^>]+)>/);
  return (m ? m[1] : s).trim().toLowerCase();
}

export function displayName(value) {
  const s = String(value || "").trim();
  const m = s.match(/^\s*"?([^"<]+?)"?\s*<[^>]+>/);
  return m ? m[1].trim() : address(s);
}

export function domainOf(value) {
  const a = address(value);
  return a.includes("@") ? a.split("@").pop() : "";
}

/** The alias a message was addressed to: `support` for `support@cybesis.com`. */
export function aliasOf(rcpt) {
  const a = address(rcpt);
  return a.includes("@") ? a.split("@")[0] : a || "unknown";
}

export function escapeHtml(text) {
  return String(text || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** One-line snippet of the body's own words: quoted history and signatures dropped. */
export function snippetOf(text, max = SNIPPET_CHARS) {
  const lines = String(text || "")
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith(">"));
  const flat = lines.join(" ").replace(/\s+/g, " ").trim();
  return flat.length > max ? flat.slice(0, max - 1) + "…" : flat;
}

/**
 * Tags for a message: the alias first, then any keyword family the subject or
 * snippet hits. Deterministic; the alias is always present so a tag is never empty.
 */
export function tagsFor({ rcpt, subject, snippet }) {
  const tags = [aliasOf(rcpt)];
  const hay = `${subject || ""} ${snippet || ""}`;
  for (const [tag, re] of KEYWORD_TAGS) {
    if (re.test(hay) && !tags.includes(tag)) tags.push(tag);
  }
  return tags;
}

/** Parse an Authentication-Results header into {spf, dkim, dmarc} verdicts ("" when absent). */
export function authResults(header) {
  const h = String(header || "").toLowerCase();
  const pick = (k) => {
    const m = h.match(new RegExp(`${k}=([a-z]+)`));
    return m ? m[1] : "";
  };
  return { spf: pick("spf"), dkim: pick("dkim"), dmarc: pick("dmarc") };
}

/** The metadata row the Worker records and reports. Never the body. */
export function buildMeta({ envelopeFrom, rcpt, headers, parsed, size }) {
  const fromHeader = (parsed && parsed.from && parsed.from.address) || headers.get("from") || envelopeFrom || "";
  const fromName = (parsed && parsed.from && parsed.from.name) || displayName(headers.get("from") || "");
  const subject = (parsed && parsed.subject) || headers.get("subject") || "";
  const text = (parsed && (parsed.text || stripHtml(parsed.html))) || "";
  const snippet = snippetOf(text);
  const auth = authResults(headers.get("authentication-results"));
  return {
    ts: new Date().toISOString(),
    alias: aliasOf(rcpt),
    rcpt: address(rcpt),
    envelope_from: address(envelopeFrom),
    from_addr: address(fromHeader),
    from_name: fromName || "",
    from_domain: domainOf(fromHeader) || domainOf(envelopeFrom),
    subject: String(subject || "").slice(0, 300),
    snippet,
    tags: tagsFor({ rcpt, subject, snippet }),
    size: Number(size || 0),
    attachments: parsed && Array.isArray(parsed.attachments) ? parsed.attachments.length : 0,
    message_id: headers.get("message-id") || "",
    ...auth,
  };
}

export function stripHtml(html) {
  if (!html) return "";
  return String(html)
    .replace(/<(script|style)\b[\s\S]*?<\/\1>/gi, " ")
    .replace(/<\s*(br|\/p|\/div|\/tr|\/li|\/h[1-6])\s*\/?>/gi, "\n")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/[ \t]+/g, " ");
}

/**
 * Is this message's authentication actually a problem worth a warning?
 *
 * `spf=none` alone is NOT: it only means the sender's domain publishes no SPF record, which
 * is common and says nothing about forgery — and DMARC can pass on the DKIM signature alone
 * (RFC 7489 §4.2: one aligned, passing mechanism is enough). Warning on `none` cried wolf on
 * ordinary authenticated mail, so it is silent now.
 *
 * A warning means: DMARC failed, or DKIM failed, or SPF failed/softfailed with no DKIM pass
 * to carry the message. An unsigned message with no SPF (`none`/`none`) and no DMARC verdict
 * is "unauthenticated", which is reported as a distinct, quieter note.
 */
export function authConcern(meta) {
  const spf = String(meta.spf || "").toLowerCase();
  const dkim = String(meta.dkim || "").toLowerCase();
  const dmarc = String(meta.dmarc || "").toLowerCase();
  if (dmarc === "fail" || dkim === "fail") return "fail";
  if ((spf === "fail" || spf === "softfail") && dkim !== "pass") return "fail";
  if (dmarc === "pass" || dkim === "pass" || spf === "pass") return "";
  return "unauthenticated";
}

/** The Telegram line (HTML parse mode): alias · sender · subject · snippet. No body, no ids. */
export function telegramText(meta) {
  const who = meta.from_name || meta.from_addr || meta.envelope_from || "?";
  const tags = meta.tags.filter((t) => t !== meta.alias);
  const tagStr = tags.length ? ` · ${tags.map((t) => `#${escapeHtml(t)}`).join(" ")}` : "";
  const att = meta.attachments ? ` · 📎${meta.attachments}` : "";
  let text = `📨 <b>${escapeHtml(meta.alias)}@</b> — ${escapeHtml(who)} · ${escapeHtml(meta.subject || "(sans objet)")}${tagStr}${att}`;
  if (meta.snippet) text += `\n<i>${escapeHtml(meta.snippet)}</i>`;
  const verdicts = [meta.spf && `spf:${meta.spf}`, meta.dkim && `dkim:${meta.dkim}`, meta.dmarc && `dmarc:${meta.dmarc}`].filter(Boolean);
  const concern = authConcern(meta);
  if (concern === "fail") text += `\n⚠️ ${escapeHtml(verdicts.join(" "))}`;
  else if (concern === "unauthenticated") text += `\nⓘ ${escapeHtml(verdicts.join(" ") || "no authentication results")}`;
  return text;
}

/**
 * Should this alias ping the operator? `NOTIFY_ALIASES` is a comma list ("support,billing");
 * "*" means every alias; empty/undefined means the default set. Everything else is still
 * forwarded and recorded — it just does not buzz a phone.
 */
export const DEFAULT_NOTIFY_ALIASES = ["support", "billing", "security", "legal", "privacy", "abuse", "partnerships", "hello", "contact", "info", "press"];

export function notifyAliasList(notifyAliases) {
  const raw = String(notifyAliases || "").trim();
  if (raw === "*") return ["*"];
  return raw ? raw.split(",").map((a) => a.trim().toLowerCase()).filter(Boolean) : DEFAULT_NOTIFY_ALIASES;
}

export function shouldNotify(alias, notifyAliases) {
  const list = notifyAliasList(notifyAliases);
  if (list[0] === "*") return true;
  return list.includes(String(alias || "").toLowerCase());
}

/** Constant-time-ish bearer comparison (lengths leak, contents do not). */
export function bearerOk(headerValue, expected) {
  const got = String(headerValue || "").replace(/^Bearer\s+/i, "").trim();
  if (!expected || got.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < got.length; i++) diff |= got.charCodeAt(i) ^ expected.charCodeAt(i);
  return diff === 0;
}

/** Aggregate rows into the /stats shape. Pure so it can be tested without D1. */
export function aggregate(rows, days) {
  const byAlias = {};
  const byTag = {};
  const byDay = {};
  const domains = {};
  let forwarded = 0;
  let notified = 0;
  for (const r of rows) {
    byAlias[r.alias] = (byAlias[r.alias] || 0) + 1;
    for (const t of String(r.tags || "").split(",").filter(Boolean)) byTag[t] = (byTag[t] || 0) + 1;
    const day = String(r.ts || "").slice(0, 10);
    if (day) byDay[day] = (byDay[day] || 0) + 1;
    if (r.from_domain) domains[r.from_domain] = (domains[r.from_domain] || 0) + 1;
    forwarded += r.forwarded ? 1 : 0;
    notified += r.notified ? 1 : 0;
  }
  const top = Object.entries(domains).sort((a, b) => b[1] - a[1]).slice(0, 10);
  return { days, total: rows.length, forwarded, notified, by_alias: byAlias, by_tag: byTag, by_day: byDay, top_domains: top };
}
