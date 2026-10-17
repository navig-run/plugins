---
name: obs-control
description: Control OBS Studio from navig — read status, switch/create scenes, show/hide sources, manage filters, start/stop streaming and recording, change stream settings, capture screenshots, and diagnose a broken OBS setup (missing files, plugins that fail to load, disabled websocket server). Use when the user mentions OBS, obs-websocket, streaming, recording, scenes, sources, studio mode, virtual camera, or wants to check/fix an OBS installation.
activation_keywords:
  - obs
  - streaming
  - recording
  - scenes
  - broadcast
# `обс` is an acronym (whole word only — it would fire inside "обсудим"). `scene` and
# `stream` are English words, so French uses phrases; `obs` itself is already English.
activation_keywords_ru: [обс, стрим, сцену, сцене, сцены, сцена, трансляц]
activation_keywords_fr: ["le stream", "mon stream", "du stream", "un stream", "de scene", "la scene", "une scene"]
---

# OBS Studio control

Drive OBS Studio over its built-in obs-websocket server (port 4455, protocol v5)
with the `navig obs` command group. Works against localhost or any machine on the
LAN (`--host`, or `NAVIG_OBS_HOST`).

## Connection checklist (do this first when a verb fails)

1. `navig obs doctor` — local health: websocket enabled? OBS running? plugins broken?
   missing scene files? Read its warnings; each carries the fix hint.
2. If the websocket server is disabled: in OBS → Tools → WebSocket Server Settings →
   enable it. `navig obs doctor` reports this exact state.
3. Password: auto-read from OBS's local config on loopback; for a remote host set
   `NAVIG_OBS_PASSWORD` (never pass `--password` in shared transcripts).

## Reading state

- `navig obs status` — version, current scene, stream/record/replay/vcam/studio state.
  Exit 0 with `○ not reachable` when OBS is simply not running (a status answer).
- `navig obs scenes list` · `navig obs items list` · `navig obs sources list`
- `navig obs stream status` · `navig obs record status`

## Acting (all --json capable)

- Scenes: `scenes create <name>` · `scenes activate <name>` · `scenes remove <name> -y`
- Items: `items list` → `items visible <id> --on/--off` · `items transform <id> --settings '{...}'`
- Sources: `sources add <name> <kind> --scene <scene>` · `sources settings <name> --set '{"file":"..."}'`
- Output: `stream start|stop` · `record start|stop` · `vcam start|stop` · `studio enable`
- Proof: `navig obs screenshot <source-or-scene> --out proof.png` — returns the saved path.
- Browser source stuck on an old page: `sources refresh <name>` (reload, no cache).
- Who hears a source: `audio monitor <name>` reads; `--type none|monitor|both` sets.
- On-stream line read by the pigeon: `show say "<text>" --tone info` (`--dry-run` first). The voice follows the loaded collection's language (en/fr/ru); `--template deploy` says the deploy line in that language; `--image https://…` puts a picture on the card; `show voices` lists/samples/keeps voices.
- Anything without a verb: `navig obs request <RequestType> --data '{...}'` (v5 names only;
  `request GetVersion` lists them under availableRequests).

## Safety

- `scenes remove`, `items remove`, `sources remove` ask for confirmation; pass `-y`
  only when the user explicitly asked for the deletion.
- `stream settings --reveal` shows the stream key — never echo it in transcripts;
  default rendering masks it.
- The stream key write path is `stream settings --set-key <key>` (reads current
  settings first and merges — the key is never printed).
