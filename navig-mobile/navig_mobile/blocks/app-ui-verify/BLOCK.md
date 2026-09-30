---
id: app-ui-verify
spec_version: 1
name: App UI Verify
version: 0.1.0
category: mobile
license: MIT
description: Open an app on a device or simulator, capture proof, and verify a screen shows the expected text — a verified app-screen receipt.
author: navig
tags: [mobile, ui, verification, agent-device, testing, receipt]
allowed-tools: Bash
target: local

requires:
  plugins:
    - name: navig-mobile
      install: pip install "navig-mobile[all]"
  tools:
    - name: agent-device
      install: npm install -g agent-device@latest      # needs Node 22+

inputs:
  - key: app
    type: string
    label: App name / bundle id / package to open
    required: true
  - key: platform
    type: enum
    label: Target platform
    required: true
    values: [ios, android, web, tvos, macos, linux]
  - key: expect_text
    type: string
    label: Text that proves the screen loaded (asserted after open)
    required: true

steps:
  - id: open
    kind: command
    safety: safe
    capabilities: [exec:navig]
    argv: [navig, mobile, ui, open, "{{inputs.app}}", -p, "{{inputs.platform}}"]
    verify:
      expect:
        exit_code: 0

  - id: snapshot
    kind: command
    safety: safe
    capabilities: [exec:navig]
    argv: [navig, mobile, ui, snapshot, -i]

  - id: proof
    kind: command
    safety: safe
    capabilities: [exec:navig]
    argv: [navig, mobile, ui, screenshot, -o, "{{workdir}}/ui-verify.png"]

verify:
  kind: command
  level: self-check
  argv: [navig, mobile, ui, assert, "{{inputs.expect_text}}", -t, "10"]
  expect:
    exit_code: 0

outputs:
  - key: proof
    label: Screenshot proving the verified screen

receipt:
  redact: []
---
# Verify an app screen loads

Opens `app` on `platform` (a booted simulator/emulator, or a device you're
authorized to drive), snapshots the accessibility tree, captures a screenshot as
proof, and **verifies the screen actually shows `expect_text`** — the machine
check that turns "it ran" into a *verified outcome*.

## Steps

1. **open** — start a `navig mobile ui` session bound to the app; the step's own
   `expect: exit_code: 0` fails fast if the app can't be launched.
2. **snapshot** — read the accessibility tree (element refs `@e1`…) so the state
   is captured, not guessed.
3. **proof** — write `ui-verify.png` into the run's workdir as visual evidence.

## Verify — and its limits

The machine verify runs `navig mobile ui assert "{{inputs.expect_text}}" -t 10`,
which snapshots the live screen and exits non-zero if the text is absent. It
**re-checks for up to 10s** so a cold app that paints its first screen a second or
two after launch is verified, not flaked — a screen that is already correct passes
immediately, so only a genuine failure waits.

The receipt records `verified` **only** when that text is present after launch — a
real end-state check, not "the command exited 0". It confirms the *expected screen
rendered*; it does not assert deeper application correctness (your own flow tests
still own that). It reads the tool's own snapshot, so it reports `self-check`.

## Requirements

This block ships with — and is discoverable through — the **navig-mobile** plugin,
and drives the **agent-device** CLI:

```
pip install "navig-mobile[all]"
npm install -g agent-device@latest      # Node 22+
navig mobile ui doctor                    # confirm the toolchain
```

## Apply

```
navig apply app-ui-verify \
  --input app=MyApp \
  --input platform=android \
  --input expect_text="Welcome"
```

Preview the exact plan without touching anything:

```
navig apply app-ui-verify --input app=MyApp --input platform=android \
  --input expect_text=Welcome --dry-run
```

Nothing here mutates a server or wipes data, so no `--approve` is needed. Driving
a **physical** device (a named real serial) additionally requires a recorded
authorization — `navig mobile consent record -u <udid> --authorization "I own this device"`.
