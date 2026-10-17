# Changelog — navig-msstore

## 0.7.1 — 2026-10-17
### Added
- **Store sales declares that it lives in Finance.** The `sales` app registers
  `merged_into="finance"` + `merged_section="Store"`, so NAVIG OS shows it as
  Finance → Store sales (and on Finance's Overview) instead of a separate tile, while
  Finance is unlocked. On a Free plan, where Finance is locked, it keeps its own tile.
  On a core that predates the fields, the plugin leaves them out and still registers.
- **Store card in the Projects hub.** A project folder with `store/identity.json` shows its
  Microsoft Store app in NAVIG OS → Projects → Links: sales and revenue for the last 30 days
  from the local sales history (no extra Partner Center calls). Needs a core with the
  Projects hub; on an older core the card is simply absent.
- **Sale alerts.** `navig mstore sales` lists paid sales (paid apps + add-on purchases)
  from Partner Center. Inside navig's daemon the plugin checks them every hour and sends a
  "Sales" notification for each new paid sale — NAVIG OS rings a cash register for it. The
  first check only records what already exists; free downloads never ring. Off with
  `navig config set plugins.msstore.sales_enabled false`.
- **Daily sales summary.** Once a day (first check after 09:00, `sales_digest_hour`) the
  daemon sends yesterday's sales by product and the last 7 days' total, to Telegram and the
  deck feed. It reuses the hourly check's data (no extra API calls), never rings, and goes
  out even on a day with no sales. Off with `navig config set plugins.msstore.sales_digest false`.
- **Late sales in the summary.** A day that grew after a summary reported it is listed the
  next morning ("Arrived late: 4 sales · $26.00 for Wed 7 Oct").
- **Store sales app.** A free desktop app in NAVIG OS (Money): daily chart, per-app totals,
  the daily summaries and the hourly check's status. Backed by a sales history the daemon
  keeps (`sales_history.json`, 400 days; 90 loaded once in the background).
- **An alert when the sales check keeps failing.** Three failed checks in a row send one
  "Sales check failing" notification with the reason (expired credentials, refused access,
  missing credentials, no network) and the fix; a later success sends "working again".

## 0.7.0 — 2026-09-28

### Added
- **Runs on its own.** `pip install navig-msstore` now gives a `navig-msstore` command with no
  navig installed: the same app navig mounts as `navig mstore`. Credentials, packaging,
  status and the add-on commands work without navig (credentials come from the
  `NAVIG_MSSTORE_*` / `AZURE_*` environment variables when the navig vault is not there).
  `publish` still applies the verified `msstore-publish` block, which needs navig, and now says
  so in one line instead of failing with "navig not found".

## 0.6.0 — 2026-09-01

First changelog entry. This package existed before this file did, so earlier
history lives in the monorepo's git log rather than being reconstructed here —
an invented history would be worse than an honest starting point.

### Packaging
- Declares its dependency on `navig` (or deliberately does not, if it runs
  standalone) and ships the full licence text in the wheel.
