# navig-antivirus

> Browser-extension malware/PUP scanning, Chrome extension-registry auditing, Chrome
> profile-index recovery, and on-demand system malware scans — wired into `navig antivirus`.

A first-party navig plugin (free, toggleable). Pure-stdlib engine (no third-party runtime deps);
Windows-focused (registry audit + Defender scan are Windows-only), extension scan/recovery work on
any OS with a Chromium-family browser.

## Why

Browser extensions are a top malware/PUP vector: free-VPN "proxyware" that routes your traffic,
coupon extensions that read your shopping activity, and outright malware (remote-code-execution
backdoors, cookie theft) installed via the Web Store or bundled installers. This plugin turns the
manual forensic sweep into repeatable `navig` commands.

## Commands

```bash
navig antivirus report                       # one-pass read-only health report
navig antivirus extensions                   # scan Chrome/Edge/Brave extensions for malware/PUP
navig antivirus extensions -b edge --min-score 5
navig antivirus registry                     # audit registry-forced extensions + policies (Windows)
navig antivirus system --type quick          # kick off a Windows Defender scan
navig antivirus system --type full
navig antivirus system --type custom --path C:\Users\me\Downloads
navig antivirus recover-profiles             # preview Chrome profile-index rebuild
navig antivirus recover-profiles --apply     # restore a collapsed profile list (close Chrome first)
```

### `extensions` — extension malware/PUP scan
Reads each profile's `Secure Preferences` (which embeds every extension's manifest + install
metadata), scores against heuristics, dedupes by id across profiles, and reports risk bands.
Flags: `proxy` (traffic MITM), all-sites `cookies`/`webRequest`, `management`, `debugger`,
`nativeMessaging`, sideloaded / non-Web-Store sources, `unsafe-eval`, and known-malware ids.
Read-only.

### `registry` — extension-registry audit (Windows)
Enumerates registry force-installed Chrome extensions (`…\Software\Google\Chrome\Extensions\<id>`)
and the `ExtensionInstallForcelist` / `ExtensionInstallBlocklist` policies. Flags bundleware
signatures: affiliate `install_parameter` (`clid=…`) and insecure `http://` update URLs. Read-only.

### `system` — on-demand malware scan
Kicks off a **Windows Defender** scan via `MpCmdRun.exe` (`quick` / `full` / `custom`). Malwarebytes
is detected and reported, but consumer Malwarebytes ships no headless scan CLI, so Defender is the
engine (this is surfaced honestly at runtime).

### `recover-profiles` — Chrome profile-index recovery
Fixes the case where Chrome's profile picker collapses to only "Default" but every `Profile N`
folder still exists on disk — the `Local State → profile.info_cache` index was pruned while the
data is intact. Rebuilds the index from each profile's own `Preferences`. Dry-run by default;
`--apply` requires the browser fully closed and backs up `Local State` first.

## Install (dev)

```bash
pip install -e plugins/navig-antivirus     # from the navig repo root
navig antivirus --help
```

## Safety

- Everything is **read-only** except `recover-profiles --apply` (which only edits the profile
  *index*, after a backup, with the browser closed) — no profile data is ever modified.
- No credentials, no network calls (Defender is invoked locally). Extension "scanning" is static
  metadata analysis, not execution.
