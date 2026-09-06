# navig-mobile

Pro-grade **Android + iOS device operations** for NAVIG — a "3uTools for both
platforms" wired natively into the `navig` CLI. Connect a phone or tablet and
inspect it, pull its data, back it up, investigate it, scan it for spyware, and
(carefully) unlock/root it — all through one command surface.

```
navig mobile devices              # every connected/paired Android + iOS device
navig mobile info                 # unified device info (auto-detects platform)
navig mobile apps list            # installed apps
navig mobile fs pull <remote> .   # pull files off the device
navig mobile screenshot           # grab the screen
navig mobile backup create        # full backup  ·  restore <src> (consent + --yes)
navig mobile doctor               # toolchain health + install guidance
navig mobile ui open MyApp -p android   # drive/verify an app UI (agent-device)
navig mobile ui snapshot -i       # a11y tree with element refs → tap @e2 · fill @e3 "…"
navig android logcat              # platform-only ops live under android/ios…
navig ios crashlogs               #   …and as convenience alias verbs
```

## Design

- **Zero hard third-party deps in the base install** — Apache-2.0 clean, and
  `navig help` stays fast. Engines load lazily only when a device is touched.
- **Two permissively-licensed Python engines**, installed via extras:
  - `pip install "navig-mobile[android]"` → [`adbutils`](https://github.com/openatx/adbutils) (Android over ADB)
  - `pip install "navig-mobile[ios]"` → [`pymobiledevice3`](https://github.com/doronz88/pymobiledevice3) (iOS over lockdown/usbmux — the "3uTools" engine)
  - `pip install "navig-mobile[all]"` → both
- **Everything else is detected, never bundled** — the `adb`/`fastboot`/`scrcpy`
  binaries, MVT (spyware), frida/objection (instrumentation), iLEAPP/ALEAPP
  (forensic parsing), and **agent-device** (app UI automation — Node, MIT).
  `navig mobile doctor` reports what's present and how to install the rest,
  honoring each tool's own license.

## Command pillars

`navig mobile --help` groups verbs by pillar: **Device & Connect · Info &
Diagnostics · Apps · Files & Pull · Backup & Restore · Screen & Control · App
Automation · Forensics & Investigate · Spyware Scan · Rooting & Jailbreak ·
Dev-Tools · OSINT & Case**.

## App UI automation & verification (`navig mobile ui …`)

The device engines *manage* a phone; **`navig mobile ui`** *drives the app on it* —
the agent's hands + eyes for verifying real app UIs. It wraps
[`agent-device`](https://github.com/callstack/agent-device) (callstack, MIT — a Node
CLI covering iOS / Android / web / desktop), **detected + driven as a subprocess**,
never bundled (so the base package stays pure-Python + Apache-2.0). Install it only if
you want UI verification: `npm install -g agent-device@latest` (Node 22+); then
`navig mobile ui doctor`.

```
navig mobile ui open MyApp -p android   # start a session bound to an app
navig mobile ui snapshot -i             # a11y tree with element refs (@e1, @e2…)
navig mobile ui tap @e2                  # act on a ref, then snapshot again
navig mobile ui fill @e3 "test@example.com"
navig mobile ui assert "Welcome"         # VERIFY the screen — exit 0/1 (machine-checkable)
navig mobile ui assert "row" --count 3   # …or an exact number of matches
navig mobile ui screenshot -o shot.png   # capture proof  (add --case <id> for evidence)
navig mobile ui close
navig mobile ui record -o flow.ad  ·  navig mobile ui replay flow.ad   # reproducible flows
navig mobile ui devices  ·  navig mobile ui apps -p ios                # discovery
```

Primary target is a **simulator/emulator** running an app you're building. `ui assert`
is the verify primitive — it snapshots the live screen and exits 0/1 so a flow is
*proven*, not eyeballed (`--gone` inverts it; `--count n` demands an exact number of
matches). It **auto-settles**: it re-checks until the assertion holds or `-t` (default
5s) runs out, so a screen that renders a beat after the tap passes instead of flaking —
a correct screen returns immediately, and only a failing assert waits (`-t 0` = check
once). Element refs match exactly, so `assert @e2` is never satisfied by `@e20`.
`snapshot`, `screenshot`, and `record` accept `--case <id>` to hash the artifact into a
case dir's chain-of-custody manifest. Add `--json` to `snapshot`/`apps`/`devices`/`assert`
for machine output; `ui exec <args>` is a raw passthrough.

**Ownership gate:** `ui open` / `ui record` refuse to drive a *physical* device (a named
real serial) without a recorded authorization (`navig mobile consent record`); simulators
and emulators are free.

### Verify a whole app screen as one outcome — the `app-ui-verify` Block

The plugin ships the **`app-ui-verify`** Block (`navig_mobile/blocks/app-ui-verify/`) —
an installable, executable **outcome**: open → snapshot → screenshot → **verify**
(`ui assert`), producing a verified receipt only when the expected screen actually
rendered. It's auto-discovered whenever navig-mobile is installed (no extra install
step), so:

```
navig apply app-ui-verify --input app=MyApp --input platform=android --input expect_text="Welcome"
```

Inspect it first with `navig block show app-ui-verify` or `navig apply … --dry-run`.

## Safety & consent

The investigative pillars (forensics, spyware-scan, private-data pull, rooting,
jailbreak, restore) are **consent-first and warn-before-destructive**. Evidence
is written read-only into a case directory with a sha256 manifest. Extracting or
scanning a device you don't own without authorization is illegal — record
authorization with `navig mobile consent` first.

## Investigate a device (consent-first)

```
navig mobile consent record -u <udid> --authorization "I own this device" --scope "..."
navig mobile forensics acquire         # iOS backup / Android bugreport → a case dir
navig mobile scan spyware              # acquire + MVT (Pegasus/mercenary IOCs)
navig mobile case list                 # your investigation cases
navig mobile evidence <case> --verify  # re-hash the chain-of-custody manifest
```

Forensics/spyware/media-pull are **consent-gated** — they refuse without a recorded
authorization for that device. Evidence is written read-only into a per-case
directory and every artifact is sha256-hashed into an append-only `manifest.json`
(who/when-UTC/where/how). MVT (spyware), iLEAPP/ALEAPP (parsing) are detected +
guided if not installed. Public IOCs are necessary-but-not-sufficient — a clean
scan is not proof a device is uncompromised.

## Root / jailbreak assist (detect → guide → drive, never one-click)

```
navig mobile root status               # Android: rooted?, bootloader lock, SELinux, Magisk
navig mobile root guide                # step-by-step Magisk unlock+patch flow
navig mobile jailbreak status          # iOS: checkm8 chip-eligibility + dev-mode
navig mobile android reboot bootloader # then, in fastboot:
navig mobile root unlock --yes         # DESTRUCTIVE — needs consent + --yes (wipes device)
navig mobile dev enable                # iOS Developer Mode / Android USB-debugging guidance
```

Wipe-level operations (`root unlock`/`flash`, `android sideload`) are **double-gated**:
a recorded consent record **and** an explicit `--yes`, with a loud warning. navig-mobile
never runs an exploit — it detects state, guides, and drives your own `fastboot` /
`pymobiledevice3` (both detected + guided if missing).

## Dev-tools & OSINT

```
navig mobile dev frida                 # list processes/apps (frida-ps)
navig mobile dev attach <app>          # frida REPL (needs frida-server on device)
navig mobile dev objection <app>       # objection explorer
navig mobile ios crashlogs             # pull iOS crash reports  · ios syslog · ios pcap
navig mobile osint device              # identity: serial/IMEI/MACs/android_id/chip-id
navig mobile osint apps                # Android app privacy report (high-risk permissions, ranked)
navig mobile osint report -o out.json  # combined identity + privacy → file (+ war-room hand-off)
```

frida / objection are detected + guided (attaching needs a matching `frida-server` on a
rooted/jailbroken device). The OSINT app scan flags high-risk permissions per app (camera,
mic, location, SMS, contacts, accessibility, all-files, install-apps…).

## Desktop-OS / deck API

The plugin serves a read-mostly device-dashboard API at `/api/deck/mobile/*` (mounted
via the `gateway:register_routes` hook, gated on the free `mobile` module) — the backend
the desktop OS renders against:

```
GET  /api/deck/mobile/devices                     # connected devices
GET  /api/deck/mobile/doctor                       # toolchain status
GET  /api/deck/mobile/device/{udid}                # device info
GET  /api/deck/mobile/device/{udid}/apps           # installed apps
GET  /api/deck/mobile/backups   ·  /cases          # inventory
POST /api/deck/mobile/device/{udid}/screenshot     # capture (path + inline preview)
```

Investigative/destructive verbs (forensics, spyware scan, root/flash, backup) are
**not** exposed over HTTP — they stay CLI-only, behind the consent gate + typed
confirmation. The visual dashboard view lives in `apps/os`.

## Status

- **Stages 0–4** (scaffold/doctor · MVP · forensics+spyware+consent · rooting/jailbreak
  · dev-tools+OSINT) and the **Stage 5 backend** (device-dashboard API + OS/deck surface
  registration) are implemented.
- The Stage 5 **frontend** (the `apps/os` React dashboard tile) is the remaining
  follow-up, owned by the desktop-OS surface (see `CHANGELOG.md`).
