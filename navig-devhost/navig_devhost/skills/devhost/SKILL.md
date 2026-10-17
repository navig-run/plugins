---
name: devhost
description: Give a local dev server a real .test domain over trusted HTTPS. Use when the user wants a custom local domain for development, an https:// URL in front of localhost, a trusted local certificate, or to stop the browser security warnings on a local dev site.
activation_keywords: [devhost, mkcert, .test]
metadata:
  version: 1.0.0
  toolsAllowed:
    - bash_exec
---

# Dev Host

This capability is provided by the **navig-devhost** plugin. Use `navig devhost
<command>` to give a local dev server a trusted HTTPS `.test` domain (hosts entry
+ mkcert certificate + a raw TLS relay in front of the plain-HTTP dev server).

- `navig devhost add <domain> --port <N>` — register a domain (dedicated loopback +
  hosts entry + mkcert cert). Needs an Administrator terminal (edits the hosts file).
- `navig devhost up [domain]` — run the HTTPS relay in the foreground (no domain = all
  registered). Start the dev server on its port first.
- `navig devhost list` / `status` — table of registered domains and their live health
  (hosts ok · cert ok · dev-server up · currently serving).
- `navig devhost remove <domain>` — remove the hosts entry, cert, and record.
- `navig devhost doctor` — check the mkcert binary, its CA, and admin prerequisites.
- Run `navig devhost --help` for the full command set. Everything supports `--json`.

A **Dev Host** section also appears automatically in `navig menu` for web projects
(set up / start / status / open / remove, right from the menu) — no install step.

Notes:
- `add` / `remove` edit the system hosts file, so run them in an elevated
  (Administrator) terminal; `up` / `list` / `status` / `doctor` do not.
- Requires [mkcert](https://github.com/FiloSottile/mkcert) installed and
  `mkcert -install` run once so the browser trusts the local CA.
