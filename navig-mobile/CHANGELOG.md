# Changelog — navig-mobile

## 0.8.0 — 2026-10-14

### Added
- **`navig unpack …` — a PC drop folder of game archives becomes per-game extractions.**
  A flat folder of `.7z/.zip/.rar/.iso`, multi-volume sets (`.7z.001+`, bare `.001`) and
  installer EXEs, turned into `_games/<Game>/` without ever moving or renaming an original:
  - `ingest` — read-only classification into `_manifest.json`: volume sets joined, console and
    non-Windows titles flagged, exact `(1)` duplicate downloads detected by size + head-hash.
  - `run` / `game` — extract one game at a time into `data.partial` → `data` (atomic; an
    interrupted game is simply redone), then the shared engine passes: Unity (incl. PC
    `<Game>_Data` installs — `_sources()` now reads them), XNB, generic bucketing
    (`extract/`, `media/`), DDS → PNG, Wwise/FMOD/XACT containers → OGG.
    **Video is excluded by default** (`--include-video` opts back in): 7z exclusion patterns
    plus a purge sweep after extraction.
  - **Nested archives** — a dump-in-archive (`.zip` inside `.7z`) is unpacked up to 3 levels;
    nested `setup_*` EXEs are tried with innoextract only (never executed).
  - **Language index** — every text file copied to `extract/loc/<lang>/` with a conservative
    path-based detector (directory names, full language words, bracketed/uppercase tags —
    `It Takes Two` and `No Man's Sky` stay `und`) + `extract/loc/_index.json`.
  - Installers: innoextract first (all languages, no execution), 7z fallback, silent install
    only behind `--allow-install`. `report` shows the batch state.
- `navig mobile unpack` engine gains PC layouts: `detect()`/`_sources()`/`is_unity()` read a
  `data/` install tree; `generic()` takes `roots=` and `skip_video=`.
- `doctor` lists `innoextract` alongside the rest of the toolchain.

## 0.7.1 — 2026-10-13

### Fixed
- **`navig mobile quest watch --install` installs for a non-elevated shell.** Windows refuses to
  register a task with a logon trigger from a normal (UAC-filtered) process — `schtasks /Create
  /SC ONLOGON` returns "Access is denied", so the auto-re-apply hook silently could not be
  installed at all. The scheduled task is still tried first (it works from an elevated shell);
  when it is refused, a per-user Startup shortcut running the same hidden `watch` command is
  written instead, and the message says which one you got. `--uninstall` now clears both.

## 0.7.0 — 2026-10-10

### Added
- **`navig mobile unpack` — extract data and media from Android games/apps.** Works on a game
  folder (`apk/`, optional `obb/`, `android-data/`), and can fill that folder from the device itself.
  - `game <package> -o DIR` — pull APK + splits, OBB data files and `Android/data/<pkg>` off the
    device, detect the engine, extract everything, write `REPORT.md`. `pull` / `run` do each half.
  - **Unity** — text assets, MonoBehaviours (IL2CPP builds via Il2CppDumper → DummyDll type trees,
    stored type trees first), textures/sprites → PNG, audio (FMOD) → WAV/OGG, video, fonts,
    meshes → OBJ, one readable CSV per script class (`reports/classes/`). Falls back to the
    player's own Unity version when bundles have it stripped.
  - **XNA/MonoGame XNB** (Stardew Valley…) — tables → JSON, textures → PNG, XACT wave banks → OGG.
  - **Kairosoft** `.bin` archives — repeating-XOR key auto-detected per title.
  - **Any other engine** (Feral, libGDX, Godot, HTML5, custom) — every file sorted into
    `extract/text` and `media/{images,audio,video,fonts,models}`, DDS → PNG, Wwise/FMOD/XACT/CRI
    containers → OGG via vgmstream, complete Ogg streams carved out of unknown archives.
  - **Civilization VI** — `civ6 DATA -o DIR` rebuilds the in-game database exactly as the game
    loads it (schema → Base → DLC/expansions by LoadOrder/criteria) and exports 38 readable lists.
  - Building blocks: `audio` (batched vgmstream+ffmpeg, ~20 files/s), `carve-ogg`, `kairo`, `xnb`,
    `dds`, `classes`, `detect`; `doctor` shows the toolchain, `setup` fetches vgmstream and
    Il2CppDumper into `~/.navig/tools`.
  - Optional extra: `pip install 'navig-mobile[unpack]'` (UnityPy, TypeTreeGeneratorAPI, lz4,
    Pillow, pyaxmlparser). 7-Zip and ffmpeg come from the OS.

### Known limits
- IL2CPP games with encrypted `global-metadata.dat` (e.g. SWGOH, Dungeon Within) export media
  but not their script data. Unreal 5 IoStore (`.utoc/.ucas`) and Total War `.pack` archives are
  listed, not decoded; Bink 2 video is kept as `.bk2`.

## 0.6.0 — 2026-10-03

### Added
- **`navig mobile quest` — Meta Quest headsets.** A Quest is an Android device, so this lives here
  rather than in a separate plugin. Commands:
  - `connect` — switch a USB-connected headset to wireless ADB (Quest builds have no "pair with
    code" screen); `connect --ip <ip>` re-attaches without a cable. The IP is remembered.
  - `status` — model, Horizon build, battery, sleep state, storage, active tweaks.
  - `tweaks [quality|balanced]` — render size, refresh rate, foveation, CPU/GPU level via
    `debug.oculus.*`, each verified by read-back; `--reset` restores defaults. The set is saved.
  - `perf` — live FPS / foveation / CPU-GPU / temperature from the compositor's `VrApi` log: the
    proof a tweak reached the running game (a `getprop` read-back only proves it was stored).
  - `awake on|off` — hold the headset awake without wearing it.
  - `mirror [--record] [--both] [--keep-awake] [--seconds N]` — scrcpy one-eye view, optional MP4;
    warns when a recording is empty because the headset was asleep.
  - `watch [--install]` — re-applies the saved tweaks whenever the headset comes back, and after a
    reboot re-enables wireless ADB as soon as it is plugged in once; `--install` runs it at
    Windows sign-in.

### Fixed
- **`adb`/`scrcpy` installed with winget were "not found"** until the shell was restarted, because
  winget's PATH edit never reaches running shells. Tool lookup now also checks winget's package
  folders, and `mirror` uses that lookup instead of a bare PATH search.

## 0.5.0 — 2026-09-28

### Added
- **Runs on its own.** `pip install navig-mobile` now gives a `navig-mobile` command with no navig
  installed: the same app navig mounts as `navig mobile` (`navig-mobile android …` / `ios …`
  included). It no longer depends on navig. The device inventory (`mobile.db`) is navig's own
  store inside navig and a same-format twin on its own, so one database serves both.

### Fixed
- **Settings saved without navig were silently discarded.** `config.set` swallowed its failure
  when navig was absent; it now writes `plugins.mobile.<key>` into the same `config.yaml` navig
  reads, and refuses (rather than overwrites) a config file it cannot read.
- The standalone data directory now honours the system-service location like navig's.
- Hints name the command that exists where you are (`navig-mobile …` on its own).

## 0.4.2 — 2026-09-04

### Fixed
- **Requires `navig>=3.25.0`.** 0.4.1 declared `navig>=3.24.0`, and the published navig 3.24.0
  does not contain modules this package imports — `core/pyproject.toml` had said 3.24.0 for 541
  commits, so the source and the wheel answering to that number were different code. Installing
  with the navig it asked for produced `ModuleNotFoundError`: immediately if the import was at
  module scope, otherwise the moment the feature ran. navig 3.25.0 contains them, and
  `scripts/check_plugin_core_floor.py` now checks every declared floor against the file list of
  the wheel it actually selects.

## [0.4.1] — 2026-07-12

### The Block now tells you the command that actually installs it
- `app-ui-verify`'s `requires:` carries the **exact** install command per requirement —
  `npm install -g agent-device@latest` for the tool, `pip install "navig-mobile[all]"` for the
  plugin. `navig block doctor app-ui-verify` and the apply gate print those verbatim instead of
  the old generic guess (`pip install navig-mobile`, which is wrong — it drops the extras — and
  "install it and put it on PATH", which is useless for an npm package). Needs the matching core
  (`policy.check_requirements`); on an older core the mapping-form entries are simply not enforced,
  exactly as before the requirements gate existed — no crash.

## [0.4.0] — 2026-07-12

### `ui assert` is now trustworthy — the verify primitive stops lying and stops flaking
- **FIXED — false-positive on element refs.** `assert` matched its needle as a plain
  substring, so `@e2` was satisfied by a screen containing only `@e20` / `@e21`: the one
  primitive whose job is *proving* a claim reported an element present that did not
  exist (and, with `--gone`, reported a removed element as still there). Element refs
  are now matched **exactly** (`@e2` never matches `@e20`); free text stays a substring
  match. Regression-tested.
- **FIXED — flaky red on a late render.** A UI is asynchronous: a screen that paints
  300ms after the tap is *correct*, not a failure, but the single-shot assert failed it.
  `assert` now **auto-settles** — it re-checks until the assertion holds or `--timeout/-t`
  (default **5s**) elapses. A screen that is already right returns immediately, so only a
  genuine failure pays the wait; `-t 0` restores single-shot. No more `ui wait` guesswork
  before an assert.
- **FIXED — a transient unreadable screen was a verdict.** A snapshot that failed
  mid-transition exited 2 ("open a session first") even when the session was fine. It is
  now retried inside the settle window and only reported if the screen never becomes
  readable.
- **NEW `--count n`** — assert an *exact* number of matches (e.g. 3 list rows).
  `--count 0` means absent; `--gone` + `--count` together is a usage error (exit 2).
- **`--json` reports more:** `count`, `expected_count`, `timeout`, `waited_seconds`
  alongside the existing `needle`/`present`/`gone_expected`/`ok` (backward-compatible).
- **The `app-ui-verify` Block inherits it** — its `verify:` now runs
  `navig mobile ui assert "<expect_text>" -t 10`, so a cold app that paints its first
  screen a second or two after launch produces a verified receipt instead of a false
  failure. SKILL.md + README updated so the agent uses the settle window instead of
  sleeping.

## [0.3.1] — 2026-07-12

### The verified-app Block is now applyable out of the box
- **Plugin-owned, auto-discovered Block.** Moved `app-ui-verify` from
  `registry/blocks/` (a marketplace mirror that never runs) into the plugin at
  `navig_mobile/blocks/app-ui-verify/BLOCK.md`, and taught core's block loader to
  discover plugin-shipped blocks (`get_block_dirs()` now unions
  `plugin_capability_dirs("blocks")`, mirroring skill discovery). Result:
  **`navig apply app-ui-verify` just works whenever navig-mobile is installed** — no
  manual `navig install add` step. The block ships in the wheel (`blocks/**/*.md`
  package-data). Registry re-vendoring removed (per "registry never re-vendors
  plugin source"). Verified end-to-end: `find_block("app-ui-verify")` resolves
  through the real plugin seam.
- **Sharper physical-device detection.** `_looks_physical` now recognizes a
  canonical UUID as an iOS/Xcode *simulator* udid (→ not gated), removing the
  false-positive where a simulator UUID tripped the consent gate. Physical serials
  (Android short serials, A12+ iOS `00008…` udids) still gate correctly.
- **Tests** — +2 plugin (package-path validation + `discover_blocks` finds it) and
  +1 core (`plugin_capability_dirs('blocks')` wired into `get_block_dirs`).

## [0.3.0] — 2026-07-12

### Stage 7 — verify the app, gate the device, ship a verified-app Block
Builds the "verification" arc on top of the Stage-6 UI-automation pillar.

- **`navig mobile ui assert <text|@ref> [--gone]`** — the missing *verify*
  primitive. Snapshots the live screen and **exits 0 if the assertion holds, 1 if
  it fails, 2 if no session is open** — turning "look/act" into "look/act/**assert**"
  so UI state becomes machine-checkable. `--json` for scripts.
- **Physical-device consent gate** — the session-starters `ui open` / `ui record`
  now require a recorded authorization when the target is a **physical** device
  (reuses the forensics consent gate → exit 4 if absent). Simulator/emulator/auto
  targets stay free (a soft ownership reminder). Heuristic: an explicitly-named
  target with no `emulator-`/`simulator`/`localhost:` marker is treated as physical
  (errs toward safety; consent is a one-liner: `navig mobile consent record`).
- **Verified-app Block** `app-ui-verify` (moved into the plugin in 0.3.1) — an
  installable, executable outcome: `open` → `snapshot` → `screenshot` (proof), with
  a top-level `verify: command` running `navig mobile ui assert "{{expect_text}}"`
  (`expect: exit_code: 0`). The receipt records **verified** only when the expected
  screen actually rendered — a real end-state check, not "the command exited 0".
  Requires the navig-mobile plugin + agent-device. Structurally linted in tests
  (`validate_block == []`).
- **Tests** — +10 (`test_uiauto.py`): the `_looks_physical` heuristic, the
  open/record consent gate (physical refused, emulator free, consent allows), the
  `ui assert` matrix (present/absent/--gone/no-session/--json), and Block validation.

## [0.2.0] — 2026-07-12

### Stage 6 — App UI automation & verification (`navig mobile ui …`)
- **New pillar: drive & verify app UIs** — the capability the device engines don't
  cover. The device engines *manage* a phone; `navig mobile ui` *drives the app on it*
  (the agent's hands + eyes): accessibility snapshots with element refs, tap/fill/type/
  scroll/press, screenshots + evidence capture, and replayable `.ad` scripts, across
  iOS / Android / web / desktop.
- **Engine: `engine/uiauto/agent_device.py`** — wraps the **agent-device** CLI
  (callstack, MIT) as a **detected subprocess**, never imported/bundled (it's Node, not
  Python) — mirrors the frida/objection/scrcpy pattern, keeping the base package
  pure-Python + Apache-2.0. Session-oriented: `open` binds a platform/device, then the
  in-session verbs act on it.
- **CLI: `navig mobile ui`** sub-Typer — `doctor · devices · apps · open · close ·
  snapshot · screenshot · tap · fill · type · scroll · press · wait · record · replay ·
  exec`. `--json` on snapshot/apps/devices; `-p/--platform` + `-u/--udid` on the entry
  verbs; a one-line ownership notice on session start.
- **Evidence integration** — `snapshot`/`screenshot`/`record` accept `--case <id>`,
  hashing the produced artifact into a case dir's append-only chain-of-custody manifest
  (reuses the Stage 2 forensics evidence store). UI evidence is dev-grade: a named case
  is created on first use (no consent gate — driving your own app in a simulator is
  benign; only physical-device driving warrants authorization).
- **Toolchain detection** — `navig mobile doctor` now reports `agent-device` and
  `node`, with install guidance (`npm install -g agent-device@latest`, Node 22+).
  `MobileToolchain.uiauto_ready`.
- **Deck / desktop-OS** — `GET /api/deck/mobile/doctor` now returns `uiauto_ready`; the
  `apps/os` Mobile dashboard shows a **UI ready/off** chip. Driving stays CLI-only
  (interactive); the dashboard is the safe glanceable view.

### Staged follow-ups
- Physical-device UI-driving behind the consent gate (currently a soft notice).
- A **verified-app Block** — emit a `BLOCK.md` that runs a UI flow and produces a
  verified receipt (Axis-1 marketplace outcome).
- iOS UI driving needs macOS/Xcode — detect-and-guide only until validated on Apple
  hardware.

## [0.1.0] — 2026-07-10

Initial release. Android + iOS device operations wired into `navig mobile`
(+ `navig android` / `navig ios`).

### Stage 0 — scaffold + toolchain
- First-party plugin skeleton (module registry entry, CLI seam, deferred gateway
  route hook), Apache-2.0, zero hard third-party deps.
- `navig mobile doctor` — detects the mobile toolchain (adbutils, pymobiledevice3,
  adb/fastboot/scrcpy, Apple usbmux driver, mvt, frida, objection, iLEAPP/ALEAPP)
  and prints install guidance for what's missing.
- `DeviceInventoryStore` (SQLite via `BaseStore`) — records seen devices +
  backup catalog.

### Stage 1 — MVP (both platforms)
- Unified `Device` abstraction; `AndroidDevice` (adbutils) + `IosDevice`
  (pymobiledevice3); `DeviceManager` discovery + auto-detect dispatch.
- Verbs: `devices`, `info`, `apps list/install/uninstall/info/extract`,
  `fs ls/pull/push`, `screenshot`, `backup create/list`.
- Full backups run as background tasks with a progress bar; the backup catalog is
  recorded in the inventory store.
- `--json` on list/status verbs; auto-resolve the single connected device, or
  `--udid` when several are attached.

### Stage 2 — forensics + spyware + consent
- **Consent-first gate** (`navig mobile consent record/show/revoke`) — investigative
  verbs (forensics, spyware scan, media pull) require a recorded authorization
  reference ("I own this device" is valid); refuses empty authorization; supports
  an expiry (`--until`).
- **Evidence case directory + chain-of-custody manifest** — every acquired/produced
  artifact is sha256-hashed on collection into an append-only `manifest.json` with
  the 5 W's (what/who/when-UTC/where/how); `navig mobile case list/show`,
  `navig mobile evidence <case> [--verify]` (re-hashes to detect tamper/missing).
- **Logical acquisition** (`navig mobile forensics acquire`) — iOS mobilebackup2
  backup / Android `adb bugreport` + property/package snapshot, into the case dir.
- **Spyware scan** (`navig mobile scan spyware/backup/iocs`) — drives MVT
  (`mvt-ios check-backup` / `mvt-android check-bugreport` — the current verbs;
  `check-adb` was removed upstream), counts IOC detections, states the
  "public IOCs are necessary-but-not-sufficient" caveat honestly.
- **Artifact parsing** (`navig mobile forensics parse`) — iLEAPP/ALEAPP →
  HTML/timeline report (detected + guided if absent).
- **Bug fix:** `fs ls` now dereferences symlinks (`ls -laL`) so a symlinked dir
  (e.g. `/sdcard`) lists its contents, and the parser skips error/`->` lines
  (found via real-device testing on a Galaxy Tab S7+).
- Store schema v2: additive `consent` + `cases` tables.

### Stage 3 — rooting / jailbreak assist (detect → guide → drive; never one-click)
- **`root status`** (Android, read-only) — rooted?, bootloader lock, verified-boot
  state, dm-verity, SELinux, Magisk app, security patch.
- **`root guide`** / **`jailbreak guide`** — honest step-by-step (Magisk unlock+patch
  flow; checkm8 chip-eligibility → palera1n/checkra1n).
- **`jailbreak status`** (iOS) — checkm8 eligibility by chip (A11-and-older) +
  developer-mode state + recommended tool.
- **Destructive ops are double-gated** (recorded consent **+** explicit `--yes`) and
  warn before wiping: **`root unlock`** (`fastboot flashing unlock`), **`root flash
  --partition --image`**, **`android sideload`**.
- **Drivable device state:** `android reboot <system|bootloader|recovery>`,
  `android fastboot <args>` (passthrough), `ios recovery enter|exit`
  (`restore enter/exit`), `dev enable` (iOS `amfi enable-developer-mode`; Android
  guidance), `ios dfu` (guided — button timing can't be automated).
- fastboot detected + guided if absent; nothing auto-roots.

### Stage 4 — dev-tools + OSINT
- **Dev-tools:** `dev frida` (list processes/apps), `dev attach <target>` /
  `dev spawn <target>` (frida REPL), `dev objection <target>` — detected + guided
  (frida-tools/objection; attach needs a matching frida-server on a rooted/
  jailbroken device). Wired the iOS diagnostics: `ios crashlogs` (`crash pull/ls`),
  `ios syslog` (`syslog live`), `ios pcap` (`pcap`).
- **OSINT (device-anchored):** `osint device` — identity (Android getprop/
  android_id; iOS lockdown serial/IMEI/MACs/chip-id). `osint apps [pkg]` — Android
  installed-app **privacy report**: flags high-risk permissions (camera, mic,
  location, SMS, contacts, accessibility, all-files, install-apps…) per app,
  ranked. `osint report` — combined identity + privacy report → a JSON file, with a
  hand-off pointer to the war-room analyst agents.
- `IosDevice.lockdown_values()` helper added. frida/objection detected +
  subprocess-invoked, never imported (heavy/native + GPLv3). Zero new deps.

### Stage 5 (backend) — desktop-OS dashboard API + surface registration
- **Gateway routes** (`gateway:register_routes` hook → `/api/deck/mobile/*`, served
  to the desktop OS + deck, each gated on the free `mobile` module):
  `GET devices · doctor · backups · cases · device/{udid} · device/{udid}/apps`
  and `POST device/{udid}/screenshot` (returns path + inline data-URI preview).
  Read-mostly by design — investigative/destructive verbs stay CLI-only behind the
  consent gate. The visual view lives in `apps/os` (owned there); this plugin ships
  the backend contract.
- Registered the `os-tile:mobile` + `deck-section:mobile` surfaces on the module.
- **CLI polish:** `logs` now dispatches (recent logcat on Android / live syslog on
  iOS); `watch` streams device arrival/removal until Ctrl-C.

### Finishing touches — the last CLI stubs
- **`restore`** — restore a backup (iOS `backup2 restore`; Android `adb restore`).
  DESTRUCTIVE: double-gated (recorded consent + explicit `--yes`) with a loud
  warning; iOS `--password` supported. `Device.restore()` added to the protocol +
  both engines.
- **`pair`** — Android wireless pairing (`adb pair <host:port> --code <code>`);
  no-arg prints platform pairing guidance.
- **`ios instruments`** — DVT passthrough (`proclist`/`applist`/`kill`/… via
  `pymobiledevice3 developer dvt`).

### Staged follow-ups (not yet shipped)
- Stage 5 (frontend) — the `apps/os` React dashboard tile (owned by the OS surface).
