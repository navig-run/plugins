# Changelog — navig-mini

## 1.1.3 — 2026-09-05

### Fixed
- **The one-line install works.** It now reads `curl -fsSL https://navig.run/mini | sh`
  (and `https://navig.run/mini.py` for the Python form). `navig.run` already serves
  `install.sh` for core, so this needed no new hostname — the previous `get.navig.run`
  has never had a DNS record, which made every documented install form fail at DNS. The
  paths 302 to the plugins mirror, so they are the same script `install.sh` already pulls
  the agent from and cannot drift from it. `get.navig.run` is attached to the same site and
  will serve the identical paths if a DNS record is ever added; nothing depends on it.
- **`install.sh` no longer starts with a byte-order mark.** It began `EF BB BF #!/bin/sh`.
  Piped to a shell that stops byte 0 being `#`, so line 1 stopped being a comment and was
  run as a command — `sh: line 1: <BOM>#!/bin/sh: No such file or directory` was the first
  line of output a new user saw, and the script then carried on, so nothing ever failed
  loudly enough to notice. Saved to disk and executed, the kernel does not recognise the
  shebang at all and refuses outright. Two guards now cover it: the piped-installer list
  this file was missing from, and a tree-wide check for a BOM in front of any shebang.

## 1.1.2 — 2026-09-03

### Fixed
- **`navig-mini init` works from a wheel.** The README advertises `pip install navig-mini`
  then `navig-mini init` as Method 2, and it could never work: the wheel ships `navig_mini/`
  only, while install.py sits at the project root, so from site-packages the file is simply
  absent. It printed "install.py not found — run: curl … https://get.navig.run/mini | sh"
  and exited 1 — pointing at a host with **no DNS record**, so the suggested recovery was
  dead too. The wizard is now fetched from the same URL install.sh already downloads
  agent.py and monitor.py from, overridable with `INSTALL_BASE_URL`. Bytes off the network
  reach `os.execv`, so a payload that does not look like Python (a 404 page, a captive
  portal) is refused rather than executed.
- **README:** `get.navig.run` has no DNS record, so both short install forms are documented
  as not-yet-resolving with a direct URL that does work.

## 1.1.1 — 2026-09-01

First changelog entry. This package existed before this file did, so earlier
history lives in the monorepo's git log rather than being reconstructed here —
an invented history would be worse than an honest starting point.

### Packaging
- Declares its dependency on `navig` (or deliberately does not, if it runs
  standalone) and ships the full licence text in the wheel.
