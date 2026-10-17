// cybesis-mailroom — the edge half of the navig mailroom.
//
// Email Routing hands every message addressed to a routed alias (support@ …) to
// `email()`. The Worker FORWARDS FIRST (delivery never depends on the extras), then in
// the background: one Telegram line to the operator, one metadata row in D1 (never the
// body), and an `X-Cybesis-Tag` header on the forwarded copy so Gmail-side rules can key
// on it. If forwarding fails the handler throws, which makes the sending server retry —
// nothing is ever dropped silently.
//
// `fetch()` is the small API navig talks to, behind a bearer secret:
//   GET  /health            liveness (no auth)
//   GET  /stats?days=7      counts by alias / tag / day / sender domain
//   GET  /events?limit=50   recent metadata rows
//   POST /send              {to, subject, text, html?, from?, reply_to?, in_reply_to?}
//                           → Email Sending binding (reply AS support@cybesis.com)
//
// No model is involved anywhere here.

import PostalMime from "postal-mime";

import { aggregate, bearerOk, buildMeta, notifyAliasList, shouldNotify, telegramText } from "./triage.js";

const TELEGRAM_API = "https://api.telegram.org";

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

async function notify(env, meta) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) return false;
  const res = await fetch(`${TELEGRAM_API}/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text: telegramText(meta), parse_mode: "HTML", disable_web_page_preview: true }),
  });
  return res.ok;
}

async function record(env, meta, forwarded, notified) {
  if (!env.DB) return;
  await env.DB.prepare(
    `INSERT INTO mail_events (ts, alias, rcpt, envelope_from, from_addr, from_name, from_domain, subject, tags, size,
       attachments, message_id, forwarded, notified, spf, dkim, dmarc)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17)`,
  )
    .bind(
      meta.ts, meta.alias, meta.rcpt, meta.envelope_from, meta.from_addr, meta.from_name, meta.from_domain,
      meta.subject, meta.tags.join(","), meta.size, meta.attachments, meta.message_id,
      forwarded ? 1 : 0, notified ? 1 : 0, meta.spf, meta.dkim, meta.dmarc,
    )
    .run();
}

export default {
  async email(message, env, ctx) {
    // The raw stream is single-use: buffer once, parse from the buffer.
    const raw = await new Response(message.raw).arrayBuffer();
    let parsed = null;
    try {
      parsed = await PostalMime.parse(raw);
    } catch (err) {
      console.warn("postal-mime failed; header-only triage", String(err));
    }
    const meta = buildMeta({ envelopeFrom: message.from, rcpt: message.to, headers: message.headers, parsed, size: raw.byteLength });

    const forwardTo = env.FORWARD_TO;
    if (!forwardTo) throw new Error("FORWARD_TO is not configured"); // 4xx → sender retries; never drop
    await message.forward(forwardTo, new Headers({ "X-Cybesis-Tag": meta.tags.join(","), "X-Cybesis-Alias": meta.alias }));

    ctx.waitUntil(
      (async () => {
        let notified = false;
        try {
          if (shouldNotify(meta.alias, env.NOTIFY_ALIASES)) notified = await notify(env, meta);
        } catch (err) {
          console.error("telegram notify failed", String(err));
        }
        try {
          await record(env, meta, true, notified);
        } catch (err) {
          console.error("d1 record failed", String(err));
        }
      })(),
    );
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/health") {
      return json({
        ok: true,
        worker: "cybesis-mailroom",
        sending: Boolean(env.EMAIL),
        domain: env.DOMAIN || "",
        forward_to: env.FORWARD_TO || "",
        notify_aliases: notifyAliasList(env.NOTIFY_ALIASES),
        read_token: Boolean(env.EDGE_READ_TOKEN),
      });
    }

    // Two scopes: a READ token may only call /stats and /events; the full token may do
    // anything (and still reads, so an install without a read token keeps working).
    // A read-only token that could also send mail AS the domain was the wrong shape.
    const auth = request.headers.get("authorization");
    const full = bearerOk(auth, env.EDGE_TOKEN);
    const readOnly = !full && bearerOk(auth, env.EDGE_READ_TOKEN);
    if (!full && !readOnly) return json({ error: "unauthorized" }, 401);

    if (request.method === "GET" && url.pathname === "/stats") {
      const days = Math.min(365, Math.max(1, Number(url.searchParams.get("days") || 7)));
      const since = new Date(Date.now() - days * 86400_000).toISOString();
      const { results } = await env.DB.prepare(
        "SELECT ts, alias, tags, from_domain, forwarded, notified FROM mail_events WHERE ts >= ?1 ORDER BY ts DESC",
      )
        .bind(since)
        .all();
      return json(aggregate(results || [], days));
    }

    if (request.method === "GET" && url.pathname === "/events") {
      const limit = Math.min(500, Math.max(1, Number(url.searchParams.get("limit") || 50)));
      const { results } = await env.DB.prepare(
        "SELECT ts, alias, rcpt, from_addr, from_name, from_domain, subject, tags, attachments, forwarded, notified, spf, dkim, dmarc FROM mail_events ORDER BY ts DESC LIMIT ?1",
      )
        .bind(limit)
        .all();
      return json({ events: results || [] });
    }

    if (request.method === "POST" && url.pathname === "/send") {
      if (readOnly) return json({ error: "this token is read-only" }, 403);
      if (!env.EMAIL) return json({ error: "Email Sending binding not configured" }, 501);
      let body;
      try {
        body = await request.json();
      } catch {
        return json({ error: "invalid JSON" }, 400);
      }
      const to = String(body.to || "").trim();
      const subject = String(body.subject || "").trim();
      const text = String(body.text || "");
      if (!to || !subject || !text) return json({ error: "to, subject and text are required" }, 400);
      const fromAddr = String(body.from || env.DEFAULT_FROM || `support@${env.DOMAIN}`).trim();
      if (!fromAddr.toLowerCase().endsWith(`@${String(env.DOMAIN).toLowerCase()}`)) {
        return json({ error: `from must be an @${env.DOMAIN} address` }, 400);
      }
      const headers = {};
      if (body.in_reply_to) {
        headers["In-Reply-To"] = String(body.in_reply_to);
        headers["References"] = String(body.references || body.in_reply_to);
      }
      const msg = {
        to,
        from: { email: fromAddr, name: String(body.from_name || env.DEFAULT_FROM_NAME || "Cybesis Studios") },
        subject,
        text,
        html: body.html ? String(body.html) : undefined,
        replyTo: body.reply_to ? String(body.reply_to) : undefined,
        headers: Object.keys(headers).length ? headers : undefined,
      };
      try {
        const result = await env.EMAIL.send(msg);
        return json({ ok: true, result });
      } catch (err) {
        return json({ ok: false, error: String(err && err.message ? err.message : err) }, 502);
      }
    }

    return json({ error: "not found" }, 404);
  },
};
