"""App UI automation & verification — driving app *user interfaces* for agentic
verification (accessibility snapshots with element refs, tap/fill/scroll, evidence
capture, replayable scripts).

The one external engine here is **agent-device** (callstack, MIT) — a Node CLI that
gives an agent "hands, eyes, and evidence" across iOS/Android/web/desktop. It is
*detected and driven as a subprocess*, never bundled/imported (it's Node, not
Python), exactly like frida/objection/scrcpy elsewhere in this plugin. This keeps
navig-mobile Apache-2.0 clean and pure-Python; only users who want UI verification
install it (`npm install -g agent-device@latest`). See `navig mobile doctor`.

This is the capability navig-mobile's device engines (adbutils / pymobiledevice3)
do *not* cover: they manage the device; agent-device drives the app on it.
"""
