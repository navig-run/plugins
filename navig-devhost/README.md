# navig-devhost

**Local `.test` domains with trusted HTTPS for any dev server — one command, any project.**

Turn `http://localhost:7645` into **`https://cybesis.test`** without per-project proxy scripts.
A first-party [navig](../../README.md) plugin (free, toggleable) that wires together the three
pieces every local dev domain needs:

1. a **hosts entry** on a dedicated loopback (coexists with your other `.test` sites on `:443`),
2. a **trusted mkcert certificate** (real padlock, no browser warnings),
3. a **raw TLS relay** in front of your plain-HTTP dev server.

No Playwright/nginx/Caddy — the relay is pure stdlib `ssl`/`socket`, and it terminates TLS then
pipes bytes verbatim, so keep-alive, SSE, and **WebSocket/HMR pass through untouched**.

## Install

```bash
py -3.13 -m pip install -e plugins/navig-devhost   # into navig's Python (Windows)
python3 -m pip install -e plugins/navig-devhost     # macOS/Linux
navig plugin list          # → navig-devhost … + wired
navig devhost doctor       # check mkcert + admin
```

Prereq: [mkcert](https://github.com/FiloSottile/mkcert) (`winget install FiloSottile.mkcert`), then
`mkcert -install` once so its CA is trusted.

## Use

```bash
# 1) register (adds hosts entry on next free 127.0.0.x + issues a cert) — needs admin
navig devhost add cybesis.test --port 7645

# 2) start your dev server however you normally do (→ http://localhost:7645)

# 3) run the relay (foreground; Ctrl+C to stop)
navig devhost up cybesis.test        # or: navig devhost up   (serves all registered)
#  🔒 https://cybesis.test  →  http://127.0.0.1:7645
```

Open **https://cybesis.test**.

## Commands

| Command | What it does |
|---|---|
| `navig devhost add <domain> --port N` | Register: dedicated loopback + hosts entry + mkcert cert. `--ip`, `--no-tls`, `--target-host`. |
| `navig devhost up [domain] [--all]` | Run the HTTPS relay (foreground). No domain → all registered. |
| `navig devhost list` / `status` | Table: hosts ok · cert ok · dev-server up · currently serving. |
| `navig devhost remove <domain>` | Remove hosts entry + cert + registry record. `--keep-cert`. |
| `navig devhost doctor` | Check mkcert, its CA, and admin for hosts edits. |

Everything supports `--json` for scripting.

## Notes / gotchas

- **Dedicated loopback per site** (`127.0.0.2`, `.3`, …) so each `.test` can own `:443` and coexist —
  matching the established house convention. `add` auto-picks the next free one.
- **Admin** is needed only for `add`/`remove` (they edit the system hosts file). `up`/`list`/`status`
  need no elevation — Windows doesn't gate ports < 1024.
- **Next.js dev**: to silence the cross-origin dev warning under the new host, add
  `allowedDevOrigins: ['<domain>']` to `next.config.js`. (Vite needs nothing.)
- State lives in `<navig config>/devhost/` (`registry.json` + `certs/`).

<sub>First-party navig plugin · Apache-2.0 · reuses navig's hosts plumbing; zero Python deps.</sub>
