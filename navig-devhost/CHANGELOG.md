# Changelog — navig-devhost

## 0.1.1 — 2026-09-04

### Fixed
- **Requires `navig>=3.25.0`.** 0.1.0 declared `navig>=3.24.0`, and the published navig 3.24.0
  does not contain modules this package imports — `core/pyproject.toml` had said 3.24.0 for 541
  commits, so the source and the wheel answering to that number were different code. Installing
  with the navig it asked for produced `ModuleNotFoundError`: immediately if the import was at
  module scope, otherwise the moment the feature ran. navig 3.25.0 contains them, and
  `scripts/check_plugin_core_floor.py` now checks every declared floor against the file list of
  the wheel it actually selects.

## Unreleased

### Fixed
- **`remove` no longer orphans a domain when the hosts removal fails.** If `hosts.remove()`
  couldn't remove the entry (not elevated, or a write error), the command still deleted the
  mkcert files and dropped the registry record — leaving the live hosts entry resolving to a
  loopback with nothing serving it and no state to retry from. It now aborts (exit 2) before
  the teardown and keeps the cert + registry intact, mirroring `add`'s abort-on-hosts-failure.
- **The TLS reverse-proxy relay no longer leaks threads and sockets on a vanished peer.**
  Each connection's two pump threads looped on `sock.recv()` with no timeout, so a peer that
  dropped without a FIN blocked `recv()` forever — the pump threads (and the handler's
  `join()`) never returned, accumulating 3 threads + 2 sockets per dead connection over a dev
  session. The pumps now use a poll timeout with a 15-minute idle deadline and honor the
  relay's stop event, so dead/idle connections are reaped.

- The `navig menu` integration moved into **navig-menu as a built-in plugin** (appears
  automatically for web projects). The wheel no longer bundles a `.mjs`, and the
  `navig devhost menu install/remove/path` commands were removed — the Python
  engine/CLI (`add`/`up`/`list`/`status`/`remove`/`doctor`) is unchanged.

## 0.1.0

Initial release. Local `.test` domains with trusted HTTPS for any dev server, from `navig devhost`.

- `add` — dedicated-loopback allocation (next free `127.0.0.x`), hosts entry (reuses navig's hosts
  plumbing, tagged for clean removal), and an mkcert trusted certificate.
- `up` — a raw TLS relay (stdlib `ssl`/`socket`): terminates TLS then pipes bytes verbatim, so
  keep-alive, SSE, and WebSocket/HMR pass through untouched. Serves one domain or all registered.
- `list` / `status` — hosts · cert · dev-server-up · currently-serving, per domain.
- `remove` — hosts entry + cert + registry record (`--keep-cert`).
- `doctor` — checks mkcert, its CA, and admin for hosts edits.
- Zero Python deps; state under `<navig config>/devhost/`. Registers the free **Dev Host** module.
