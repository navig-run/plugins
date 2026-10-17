# Changelog

## 0.5.0 — 2026-10-15

- **Added** `navig obs show say TEXT`: the Grid Pigeon speaks a line — TTS
  through `navig voice speak` (any `--provider`, default edge) cached under
  `~/.navig/obs-show/tts/<sha16>.mp3` and never re-synthesized, length via
  ffprobe; then in one OBS session a `schema-toast` alert card, the `pigeon`
  `{act:"speak", ms}` event and the clip through the one cue input
  (`actions.play_file`, shared with `play_cue`). A failed synthesis still shows
  the card (warning, exit 0). `--dry-run` touches nothing.
- **Added** `obs.show.civic_say` (default off — TTS can bill): a civic beat is
  also spoken (`"SAINT PETERSBURG has ignited."`) through the same say path, by
  the bridge and by `show civic --fire`. `obs.show.say_provider` / `say_voice`.
- **Added** `navig obs request TYPE [--data JSON]` (raw obs-websocket v5
  passthrough), `navig obs sources refresh NAME` (browser source reload without
  cache), `navig obs audio monitor NAME [--type none|monitor|both]`.

## 0.4.0 — 2026-10-15

- **Added** the civic feed to the show bridge: Schema's public
  `/api/civic/events` → a `pigeonCivic` obs-browser `emit_event` per new row
  (seed-first, cursor never backwards, at most three after downtime), run by
  the same gateway service as the patch feed (`obs.show.civic_feed`).
- **Added** `navig obs show pigeon <beat> [--mode]` (the `pigeon` page event)
  and `navig obs show civic [--fire]`.
- **Changed** `actions.emit_event` is the one page-event move;
  `emit_toast` now calls it.

## 0.3.0 — 2026-10-15

- **Added** show events (`navig_obs.show`, `navig obs show status|setup|cue|toast|poll`):
  Schema's Source log feed → an on-screen `schema-toast` (obs-browser
  `emit_event`) + the `patch-landed` cue; navig's `sale` notification → the
  `sale-ring` cue. Runs inside the gateway (`gateway:register_routes` hook) when
  `obs.show.enabled` is on; `GET /api/deck/obs/show` reports it. Cues come from a
  contract-04 cue book and play through one media input (one playback path).

## 0.2.0 — 2026-10-14

- **Added** `navig obs session start|stop`: on-disk recording sessions —
  `session.json` anchors the recording clock (scene, collection, canvas,
  portrait flag, lang) and `session.json`/`markers.jsonl` are the writer of
  SCHEMA broadcast **contract 7** (`design/broadcast/contracts/`).
  `stop` sets `endEpoch` and locates the recording file written since
  `startEpoch`. Sessions live under `~/.navig/obs-sessions/<id>/` (media never
  enters a repository); `--dir` / `NAVIG_OBS_SESSIONS` override the root.
- **Added** `navig obs marker <label> --kind …`: appends
  `{tOffset, wallClock, label, kind}` to the session's `markers.jsonl` while
  OBS is recording (tOffset from `GetRecordStatus.outputDuration`);
  `--session <dir>` or `NAVIG_OBS_SESSION` selects the session.
  Usage errors (no session, bad kind, empty label) exit 2; "not recording" and
  missing records exit 1.

## 0.1.0 — 2026-10-14

- **Added** `navig obs` / `navig-obs`: full obs-websocket v5 controller —
  scenes, scene items, sources, filters, transitions, stream, record, replay
  buffer, virtual camera, studio mode, scene collections, profiles, media
  playback, hotkeys, screenshots, and live event streaming (`events --json`).
- **Added** `navig obs doctor`: read-only local health check — OBS install,
  websocket server state, plugin binaries vs configs, newest-log error lines,
  and scene-collection file references that no longer exist (the OBS
  "Missing Files" dialog).
- **Added** target resolution: flags > `NAVIG_OBS_*` env > navig `obs.*` config >
  OBS's own local websocket config (loopback) > defaults; stream key masked
  everywhere except an explicit `--reveal`.
