import assert from "node:assert/strict";
import { test } from "node:test";

import {
  address, aggregate, aliasOf, authConcern, authResults, bearerOk, buildMeta, displayName,
  domainOf, notifyAliasList, shouldNotify, snippetOf, tagsFor, telegramText,
} from "../src/triage.js";

test("addresses", () => {
  assert.equal(address("Bob <Bob@Spam.IO>"), "bob@spam.io");
  assert.equal(displayName('"Bob Q" <bob@spam.io>'), "Bob Q");
  assert.equal(displayName("bob@spam.io"), "bob@spam.io");
  assert.equal(domainOf("Bob <bob@spam.io>"), "spam.io");
  assert.equal(aliasOf("support@cybesis.com"), "support");
});

test("snippet drops quotes and caps length", () => {
  const s = snippetOf("Hello there\n> quoted line\n\nSecond " + "x".repeat(300));
  assert.ok(s.startsWith("Hello there Second"));
  assert.equal(s.length, 120);
  assert.ok(s.endsWith("…"));
});

test("tags: alias first, then keyword families", () => {
  assert.deepEqual(tagsFor({ rcpt: "support@cybesis.com", subject: "Hi", snippet: "" }), ["support"]);
  assert.deepEqual(
    tagsFor({ rcpt: "billing@cybesis.com", subject: "Facture impayée — date limite", snippet: "urgent" }),
    ["billing", "echeance", "facture", "urgent"],
  );
});

test("authentication-results parsing", () => {
  assert.deepEqual(authResults("mx.cloudflare.net; dkim=pass header.d=x; spf=softfail smtp.mailfrom=y; dmarc=none"),
    { spf: "softfail", dkim: "pass", dmarc: "none" });
  assert.deepEqual(authResults(""), { spf: "", dkim: "", dmarc: "" });
});

test("buildMeta prefers parsed fields and never carries the body", () => {
  const headers = new Headers({ from: "Env <env@x.io>", subject: "H", "message-id": "<m@x>", "authentication-results": "spf=pass dkim=pass dmarc=pass" });
  const parsed = { from: { name: "Client", address: "c@client.fr" }, subject: "Need help <urgent>", text: "Bonjour,\n\n> old\nmy secret body", attachments: [{}] };
  const meta = buildMeta({ envelopeFrom: "bounce@client.fr", rcpt: "support@cybesis.com", headers, parsed, size: 1234 });
  assert.equal(meta.alias, "support");
  assert.equal(meta.from_addr, "c@client.fr");
  assert.equal(meta.from_name, "Client");
  assert.equal(meta.attachments, 1);
  assert.deepEqual(meta.tags, ["support", "urgent"]);
  assert.ok(!("body" in meta) && !("text" in meta));
  const tg = telegramText(meta);
  assert.ok(tg.includes("<b>support@</b>") && tg.includes("&lt;urgent&gt;") && tg.includes("#urgent") && tg.includes("📎1"));
  assert.ok(!tg.includes("⚠️"));
  const bad = telegramText({ ...meta, spf: "fail", dkim: "", dmarc: "none" });
  assert.ok(bad.includes("⚠️ spf:fail dmarc:none"));
});

test("buildMeta falls back to headers when parsing failed", () => {
  const headers = new Headers({ from: "Bob <bob@spam.io>", subject: "Rank #1" });
  const meta = buildMeta({ envelopeFrom: "bob@spam.io", rcpt: "hello@cybesis.com", headers, parsed: null, size: 10 });
  assert.equal(meta.from_name, "Bob");
  assert.equal(meta.subject, "Rank #1");
  assert.deepEqual(meta.tags, ["hello"]);
});

test("bearer compare", () => {
  assert.ok(bearerOk("Bearer abc123", "abc123"));
  assert.ok(!bearerOk("Bearer abc124", "abc123"));
  assert.ok(!bearerOk("", "abc123"));
  assert.ok(!bearerOk("Bearer abc123", ""));
});

test("aggregate", () => {
  const rows = [
    { ts: "2026-09-20T10:00:00Z", alias: "support", tags: "support,urgent", from_domain: "a.io", forwarded: 1, notified: 1 },
    { ts: "2026-09-20T11:00:00Z", alias: "support", tags: "support", from_domain: "a.io", forwarded: 1, notified: 0 },
    { ts: "2026-09-19T11:00:00Z", alias: "billing", tags: "billing,facture", from_domain: "b.fr", forwarded: 1, notified: 1 },
  ];
  const st = aggregate(rows, 7);
  assert.equal(st.total, 3);
  assert.deepEqual(st.by_alias, { support: 2, billing: 1 });
  assert.equal(st.by_tag.support, 2);
  assert.equal(st.by_day["2026-09-20"], 2);
  assert.deepEqual(st.top_domains[0], ["a.io", 2]);
  assert.equal(st.notified, 2);
});

test("notify policy per alias", () => {
  assert.ok(shouldNotify("support", ""));
  assert.ok(!shouldNotify("spam", ""));
  assert.ok(!shouldNotify("sergey", undefined));
  assert.ok(shouldNotify("spam", "*"));
  assert.ok(shouldNotify("billing", "billing, support"));
  assert.ok(!shouldNotify("support", "billing"));
});

test("authConcern: spf=none with DKIM+DMARC pass is NOT a problem", () => {
  // The line that cried wolf on ordinary mail: a sender with no SPF record whose
  // DKIM signature passes and whose DMARC therefore passes is authenticated.
  assert.equal(authConcern({ spf: "none", dkim: "pass", dmarc: "pass" }), "");
  assert.equal(authConcern({ spf: "pass", dkim: "pass", dmarc: "pass" }), "");
  assert.equal(authConcern({ spf: "pass", dkim: "none", dmarc: "none" }), "");

  assert.equal(authConcern({ spf: "none", dkim: "fail", dmarc: "fail" }), "fail");
  assert.equal(authConcern({ spf: "softfail", dkim: "none", dmarc: "none" }), "fail");
  assert.equal(authConcern({ spf: "fail", dkim: "pass", dmarc: "pass" }), ""); // DKIM carries it
  assert.equal(authConcern({}), "unauthenticated");
});

test("telegramText: warns only on a real failure, notes an unauthenticated message", () => {
  const base = { alias: "support", from_name: "A", subject: "S", tags: ["support"], snippet: "" };
  assert.ok(!telegramText({ ...base, spf: "none", dkim: "pass", dmarc: "pass" }).includes("⚠️"));
  assert.ok(telegramText({ ...base, spf: "fail", dkim: "none", dmarc: "fail" }).includes("⚠️ spf:fail"));
  const note = telegramText({ ...base, spf: "none", dkim: "none", dmarc: "none" });
  assert.ok(note.includes("ⓘ") && !note.includes("⚠️"));
});

test("notifyAliasList is what /health echoes", () => {
  assert.deepEqual(notifyAliasList("support, Billing ,"), ["support", "billing"]);
  assert.deepEqual(notifyAliasList("*"), ["*"]);
  assert.ok(notifyAliasList("").includes("support"));
  assert.ok(shouldNotify("anything", "*"));
});
