"""Show events: put what happens off-stream on the stream.

Four things reach OBS through here, and only these four:

- **A deploy** → the Schema Source log feed (``obs.show.patch_feed``) gains a
  compiling or published patch → an on-screen toast (an ``obs-browser``
  ``emit_event`` the broadcast overlay listens for) + the ``patch-landed`` cue.
- **A store sale** → navig's ``sale`` notification (navig-msstore) → the
  ``sale-ring`` cue. Sound only: a sale never puts text on screen.
- **A civic event** -> the public civic feed (``obs.show.civic_feed``) gains a
  row -> a ``pigeonCivic`` page event; the Grid Pigeon reacts to its kind.
  By default no text and no sound: the bird is the whole reaction. With
  ``obs.show.civic_say`` on, the beat is also spoken (the line below).
- **A spoken line** (``navig obs show say``) -> a cached TTS clip -> an alert
  card + the pigeon's ``speak`` act + the clip through the cue input.

Every sound plays through ONE media input (``obs.show.cue_input``) — the single
playback path of the broadcast cue contract (contract-04) — so two cues can
never double-trigger. Off by default (``obs.show.enabled``); an OBS that is not
running is a quiet no-op, never an error loop.
"""
