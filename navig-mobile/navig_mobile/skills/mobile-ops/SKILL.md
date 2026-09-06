---
name: mobile-ops
description: >-
  Operate connected Android and iOS phones/tablets through the `navig mobile`
  CLI (plus `navig android` / `navig ios`). Use whenever the user wants to work
  with a plugged-in mobile device: "connect my phone", "list installed apps",
  "pull photos/contacts/files off the phone", "back up my iPhone/Android",
  "take a screenshot of the device", "what device is connected", "check the
  phone", "mirror my Android screen", "is this phone rooted/jailbroken", or asks
  about device forensics / spyware scanning / rooting. ALSO use for driving or
  verifying an app's UI — "open the app and tap login", "verify this screen",
  "check my app builds/runs on the emulator", "test this flow on the simulator",
  "record/replay a UI flow" — via `navig mobile ui …` (agent-device). Start with `navig mobile
  doctor` to confirm the toolchain, then `navig mobile devices`. Read-only verbs
  (devices, doctor, info, apps list, fs ls, screenshot) are safe; install/
  uninstall/push/backup/restore mutate the device — confirm target first.
safety: elevated
category: devices
platforms: [windows, macos, linux]
tags: [android, ios, adb, iphone, ipad, device, backup, forensics, mobile, phone, tablet]
---

# Mobile device operations (`navig mobile`)

Drive connected Android + iOS devices. One umbrella verb auto-detects the single
connected device; pass `--udid <id>` when several are attached (`navig mobile
devices` lists ids). Add `--json` to any list/status verb for machine output.

## First, always
1. `navig mobile doctor` — confirms which engines/binaries are present and prints
   install hints for anything missing (Android needs `navig-mobile[android]` +
   `adb`; iOS needs `navig-mobile[ios]` + the Apple usbmux driver).
2. `navig mobile devices` — every connected/paired device.

## Core verbs (both platforms)
- `navig mobile info [-u <udid>]` — name, model, OS, battery, storage, root/JB.
- `navig mobile apps list [--system]` · `apps install <apk|ipa>` · `apps
  uninstall <id>` · `apps info <id>` · `apps extract <id> -o <dir>`.
- `navig mobile fs ls <remote>` · `fs pull <remote> <local>` · `fs push <local> <remote>`.
- `navig mobile screenshot [-o out.png]`.
- `navig mobile backup create [-o <dir>]` · `backup list` · `restore <src>` (DESTRUCTIVE — consent + `--yes`).
- `navig mobile mirror` — scrcpy screen mirror (Android).

## Platform-only
- `navig mobile android logcat [-n 200]` · `android connect <host:port>`.
- `navig mobile ios crashlogs` · `ios syslog` (staged).

## App UI automation & verification (`navig mobile ui …`)
Drive and verify an app's UI — the agent's hands + eyes on the screen (distinct from
the device-management verbs above). Wraps the detected **agent-device** CLI (MIT); if
absent, `navig mobile ui doctor` prints how to install it (`npm install -g
agent-device@latest`, Node 22+). Primary target is a simulator/emulator running an app
you're building; only drive a physical device you own or are authorized to test.

The loop is **look → act → verify**:
1. `navig mobile ui open <app> -p <ios|android|web|macos|linux>` — start a session.
2. `navig mobile ui snapshot -i` — accessibility tree with element refs (`@e1`, `@e2`…).
3. `navig mobile ui tap @e2` · `ui fill @e3 "text@example.com"` · `ui type "…"` ·
   `ui scroll down` · `ui press back` — act on a ref, then snapshot again to see the result.
4. `navig mobile ui assert "<text|@ref>" [--gone] [--count n] [-t secs]` — **verify** the
   screen shows (or hides) something. Exits 0 if the assertion holds, 1 if it fails —
   the machine-checkable proof that a flow worked. Use this instead of eyeballing a
   snapshot. It **auto-settles**: it re-checks until the assertion holds or `-t`
   (default 5s) runs out, so a screen that renders a beat after the tap passes rather
   than flaking — no manual `ui wait` before an assert. A correct screen returns at
   once; only a failing assert waits. `--count 3` asserts an exact number of matches
   (e.g. list rows); `-t 0` checks once, no waiting. Element refs match exactly, so
   `assert @e2` is never satisfied by `@e20`.
5. `navig mobile ui screenshot -o shot.png` — capture proof.
6. `navig mobile ui close` — end the session.

Discovery: `navig mobile ui devices` (targets), `navig mobile ui apps -p android` (apps).
Replay: `navig mobile ui record -o flow.ad` (record a flow) · `ui replay flow.ad`.
Evidence: add `--case <id>` to `snapshot`/`screenshot`/`record` to hash the artifact
into a case dir's chain-of-custody manifest (`navig mobile evidence <case> --verify`).
Escape hatch: `navig mobile ui exec <raw agent-device args>`. Add `--json` to
`snapshot`/`apps`/`devices`/`assert` for machine output.

**Ownership gate:** `ui open`/`ui record` refuse to drive a **physical** device (a
named real serial) without a recorded authorization — run `navig mobile consent record
-u <udid> --authorization "I own this device"` first. Simulators/emulators need no consent.

**Verify a whole app screen as one outcome:** the **`app-ui-verify` Block** does
open → snapshot → screenshot → assert and emits a verified receipt:
`navig apply app-ui-verify --input app=MyApp --input platform=android --input expect_text="Welcome"`.

## Investigate a device (consent-first)
These **require recorded authorization first** and will refuse otherwise:
1. `navig mobile consent record -u <udid> --authorization "I own this device" --scope "..."`
2. `navig mobile forensics acquire` — iOS backup / Android bugreport → a case dir.
3. `navig mobile scan spyware` — acquire + MVT (Pegasus/mercenary-spyware IOCs).
4. `navig mobile case list` · `navig mobile evidence <case> --verify` (chain-of-custody).
- `navig mobile media -u <udid>` — pull the camera roll (consent-gated).
- MVT / iLEAPP / ALEAPP are detected; if absent, the command prints how to install.
- A clean MVT scan is **not** proof a device is uncompromised (public IOCs only).

## Root / jailbreak (detect → guide → drive; never one-click)
- `navig mobile root status` (Android) / `navig mobile jailbreak status` (iOS) — read-only
  state: rooted?, bootloader lock, SELinux; or iOS checkm8 eligibility + dev-mode.
- `navig mobile root guide` / `jailbreak guide` — the safe step-by-step.
- `navig mobile android reboot bootloader` → `navig mobile root unlock --yes` — **wipes the
  device**; needs a recorded consent record AND explicit `--yes`.
- `navig mobile root flash --partition boot --image <patched> --yes` · `android sideload <zip>`.
- `navig mobile dev enable` (iOS Developer Mode / Android USB-debugging) · `ios recovery enter|exit`.
- fastboot is detected + guided; navig-mobile never runs an exploit.

## Dev-tools & OSINT
- `navig mobile dev frida` (list procs) · `dev attach <app>` / `dev spawn <app>` (frida REPL) ·
  `dev objection <app>` — detected + guided; attaching needs a frida-server on a rooted/JB device.
- `navig mobile ios crashlogs` (pull crash reports) · `ios syslog` · `ios pcap` (packet capture).
- `navig mobile osint device` — identity (serial/IMEI/MACs/android_id/chip-id).
- `navig mobile osint apps [<pkg>]` — Android app **privacy report**: high-risk permissions per app, ranked.
- `navig mobile osint report -o <file>` — combined report → JSON, with a war-room analyst hand-off.

## Safety
`fs pull` of private data, `backup`, `install`/`uninstall`/`restore`, `media`, and the
forensics/spyware pillars are **elevated** and enforce a consent record. Rooting/jailbreak
**wipe-level** ops (`root unlock`/`flash`, `android sideload`) are **double-gated**: consent
record + explicit `--yes`, with a loud warning. Only operate a device you own or are
authorized to inspect.

Never invent a udid or path — read `navig mobile devices` / `navig mobile fs ls`
first. Warn before anything that writes to or wipes the device.
